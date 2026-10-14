"""Employee Attendance & Analytics API."""
import os, re, calendar, json
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30)); UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")

@app.on_event("startup")
def startup():
    indexes()


def now_utc(): return datetime.now(UTC).replace(microsecond=0)
def trunc_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)
def from_ms(v: int) -> datetime:
    if isinstance(v, bool) or not isinstance(v, int) or v < 100_000_000_000 or v > 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds")
    try: return datetime.fromtimestamp(v / 1000, UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError): raise HTTPException(422, "invalid timestamp")
def to_ms(v):
    if v is None: return None
    if v.tzinfo is None: v = v.replace(tzinfo=UTC)
    return int(v.timestamp() * 1000)
def round_half(value, places=2):
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))
def shift_datetime(day: str, hhmm: str, next_day=False):
    d = date.fromisoformat(day) + (timedelta(days=1) if next_day else timedelta())
    h,m = map(int, hhmm.split(':'))
    return datetime.combine(d, time(h,m), IST).astimezone(UTC)
def attendance_day(punch_in: datetime, emp):
    local = punch_in.astimezone(IST)
    start_h, start_m = map(int, emp['shift_start'].split(':'))
    end_h, end_m = map(int, emp['shift_end'].split(':'))
    overnight = (end_h, end_m) <= (start_h, start_m)
    d = local.date()
    if overnight and (local.hour, local.minute) < (end_h, end_m): d -= timedelta(days=1)
    return d.isoformat()
def compute_late_minutes(punch_in, shift_start):
    local = punch_in.astimezone(IST); h,m=map(int,shift_start.split(':'))
    start=local.replace(hour=h,minute=m,second=0,microsecond=0)
    delta=(local-start).total_seconds()
    return int(delta//60) if delta > 600 else 0
def compute_work_hours(punch_in, punch_out):
    # Use integer whole seconds and Decimal division to avoid float/bankers-rounding errors.
    delta = punch_out - punch_in
    seconds = delta.days * 86400 + delta.seconds
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
def compute_overtime(punch_out, shift_end, date_str, shift_start=None):
    sh,sm=map(int,shift_end.split(':')); start=(0,0)
    if shift_start: start=tuple(map(int,shift_start.split(':')))
    overnight=bool(shift_start and (sh,sm)<=start)
    end=shift_datetime(date_str, shift_end, overnight)
    delta = punch_out - end
    seconds = delta.days * 86400 + delta.seconds
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0
def derive(doc, emp):
    status=doc['status']; pin=doc.get('punch_in'); pout=doc.get('punch_out')
    if status not in PRESENCE:
        doc.update(punch_in=None,punch_out=None,work_hours=None,late_minutes=0,overtime_minutes=0,half_day=False)
    else:
        doc['late_minutes']=compute_late_minutes(pin,emp['shift_start']) if pin else 0
        if pin and pout:
            doc['work_hours']=compute_work_hours(pin,pout)
            doc['overtime_minutes']=compute_overtime(pout,emp['shift_end'],doc['date'],emp['shift_start'])
            doc['half_day']=doc['work_hours'] < 4.5
        else: doc.update(work_hours=None,overtime_minutes=0,half_day=False)
    return doc

def public_doc(doc):
    d={k:v for k,v in doc.items() if k != '_id'}
    if 'created_at' in d: d['created_at']=to_ms(d['created_at'])
    for k in ('punch_in','punch_out'):
        if k in d: d[k]=to_ms(d[k])
    d.setdefault('history',[]); d.setdefault('half_day',False); d.setdefault('late_minutes',0); d.setdefault('overtime_minutes',0)
    for h in d['history']:
        h['at']=to_ms(h.get('at'))
        for change in h.get('changes',{}).values():
            for side in ('from','to'):
                if isinstance(change.get(side),datetime): change[side]=to_ms(change[side])
    return d

def valid_date(s):
    if not isinstance(s, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
        raise HTTPException(422, 'date must be YYYY-MM-DD')
    try: return date.fromisoformat(s)
    except (TypeError, ValueError): raise HTTPException(422, 'date must be YYYY-MM-DD')
def valid_month(s):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',s): raise HTTPException(422,'month must be YYYY-MM')
    y,m=map(int,s.split('-')); first=date(y,m,1); last=date(y,m,calendar.monthrange(y,m)[1]); return first,last
def month_filter(month):
    first,last=valid_month(month)
    return {"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}}, first,last
def working_days(start: date, end: date):
    return sum(1 for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday()<5)
def present_expr():
    return {"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}
def mongo_round_half_up(expr, places=4):
    # Attendance rates are non-negative; floor(x * scale + 0.5) implements half-up.
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, 0.5]}}, scale]}

def clean_mongo(x):
    # Ensure BSON values nested in explain documents can be JSON returned by FastAPI.
    return json.loads(json_util.dumps(x))

class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code: str = Field(pattern=r'^EMP\d{4,6}$')
    name: str = Field(min_length=1,max_length=100)
    email: str = Field(max_length=120,pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(min_length=1,max_length=50)
    shift_start: str = '09:30'
    shift_end: str = '18:30'
    joined_on: str
    @field_validator('shift_start','shift_end')
    @classmethod
    def shift_format(cls,v):
        if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d',v): raise ValueError('time must be HH:MM')
        return v
    @field_validator('joined_on')
    @classmethod
    def date_format(cls,v):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v): raise ValueError('joined_on must be YYYY-MM-DD')
        date.fromisoformat(v)
        return v
    @model_validator(mode='after')
    def shift_diff(self):
        if self.shift_start == self.shift_end: raise ValueError('shift_start must differ from shift_end')
        return self
class PunchInIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code: str
    punched_at: Optional[int] = Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
    status: Literal['PRESENT','WFH','ON_DUTY']='PRESENT'
    @field_validator('punched_at',mode='before')
    @classmethod
    def timestamp(cls,v):
        if v is not None and (isinstance(v,bool) or not isinstance(v,int)): raise ValueError('must be epoch milliseconds integer')
        return v
class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code: str
    punched_at: Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
    @field_validator('punched_at',mode='before')
    @classmethod
    def timestamp(cls,v):
        if v is not None and (isinstance(v,bool) or not isinstance(v,int)): raise ValueError('must be epoch milliseconds integer')
        return v
class RegularizeIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    status: Optional[Literal['PRESENT','WFH','ON_DUTY','ABSENT','LEAVE']]=None
    punch_in: Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
    punch_out: Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
    reason: str=Field(min_length=5,max_length=200)
    regularized_by: str=Field(min_length=1,max_length=50)
    @field_validator('punch_in','punch_out',mode='before')
    @classmethod
    def timestamp(cls,v):
        if v is not None and (isinstance(v,bool) or not isinstance(v,int)): raise ValueError('must be epoch milliseconds integer')
        return v

@app.get('/health')
def health():
    try: client.admin.command('ping'); return {'status':'ok'}
    except Exception: raise HTTPException(503,'MongoDB unavailable')

@app.post('/employees',status_code=201)
def create_employee(body: EmployeeIn):
    doc=body.model_dump(); doc['created_at']=now_utc()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'emp_code already exists')
    return public_doc(doc)

@app.get('/employees')
def list_employees(department: Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={'department':department} if department is not None else {}
    total=db.employees.count_documents(q)
    items=[public_doc(x) for x in db.employees.find(q,{'_id':0}).sort('emp_code',ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {'items':items,'total':total,'page':page,'page_size':page_size}

@app.post('/attendance/punch-in',status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    ts=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    d=attendance_day(ts,emp)
    doc={'emp_code':body.emp_code,'date':d,'status':body.status,'punch_in':ts,'punch_out':None,'work_hours':None,'late_minutes':compute_late_minutes(ts,emp['shift_start']),'overtime_minutes':0,'half_day':False,'history':[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'already punched in for this date')
    return public_doc(doc)

@app.post('/attendance/punch-out')
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    ts=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec=db.attendance_logs.find_one({'emp_code':body.emp_code,'punch_in':{'$lte':ts},'status':{'$in':list(PRESENCE)}},sort=[('punch_in',DESCENDING)])
    if not rec: raise HTTPException(404,'no punch-in found')
    if rec.get('punch_out') is not None: raise HTTPException(409,'record already punched out')
    if ts <= rec['punch_in'] or ts > rec['punch_in']+timedelta(hours=24): raise HTTPException(422,'punch-out must be after punch-in and within 24 hours')
    hours=compute_work_hours(rec['punch_in'],ts); ot=compute_overtime(ts,emp['shift_end'],rec['date'],emp['shift_start'])
    update={'punch_out':ts,'work_hours':hours,'overtime_minutes':ot,'half_day':hours<4.5}
    updated=db.attendance_logs.find_one_and_update({'_id':rec['_id'],'punch_out':None},{'$set':update},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,'record already punched out')
    return public_doc(updated)

@app.get('/attendance')
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[Literal['PRESENT','WFH','ON_DUTY','ABSENT','LEAVE']]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from: valid_date(date_from)
    if date_to: valid_date(date_to)
    if date_from and date_to and date_from>date_to: raise HTTPException(422,'date_from must be <= date_to')
    q={}
    if emp_code: q['emp_code']=emp_code
    if status: q['status']=status
    if date_from or date_to:
        q['date']={}
        if date_from:q['date']['$gte']=date_from
        if date_to:q['date']['$lte']=date_to
    total=db.attendance_logs.count_documents(q)
    docs=db.attendance_logs.find(q,{'_id':0}).sort([('date',DESCENDING),('emp_code',ASCENDING)]).hint('attendance_date_employee').skip((page-1)*page_size).limit(page_size)
    return {'items':[public_doc(d) for d in docs],'total':total,'page':page,'page_size':page_size}

@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code:str,date:str,body:RegularizeIn):
    valid_date(date)
    emp=db.employees.find_one({'emp_code':emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    changes_in=body.model_fields_set-{'reason','regularized_by'}
    if not changes_in: raise HTTPException(422,'no changes supplied')
    rec=db.attendance_logs.find_one({'emp_code':emp_code,'date':date})
    if not rec: raise HTTPException(404,'attendance record not found')
    new={k:rec.get(k) for k in ('status','punch_in','punch_out','work_hours','late_minutes','overtime_minutes','half_day')}
    if 'status' in changes_in:
        if body.status is None: raise HTTPException(422, 'status cannot be null')
        new['status']=body.status
    for field in ('punch_in','punch_out'):
        if field in changes_in:new[field]=from_ms(getattr(body,field)) if getattr(body,field) is not None else None
    if new['status'] in ('ABSENT','LEAVE'):
        if ('punch_in' in changes_in and body.punch_in is not None) or ('punch_out' in changes_in and body.punch_out is not None): raise HTTPException(422,'ABSENT/LEAVE cannot have punch times')
        new.update(punch_in=None,punch_out=None)
    else:
        if not new.get('punch_in'): raise HTTPException(422,'presence status requires punch_in')
        if attendance_day(new['punch_in'],emp)!=date: raise HTTPException(422,'punch_in must fall on attendance date')
        if new.get('punch_out'):
            if new['punch_out']<=new['punch_in'] or new['punch_out']>new['punch_in']+timedelta(hours=24): raise HTTPException(422,'punch_out must be after punch_in and within 24 hours')
    newdoc={'emp_code':emp_code,'date':date,**new}
    derive(newdoc,emp)
    actual={k:{'from':rec.get(k),'to':newdoc.get(k)} for k in newdoc if k in ('status','punch_in','punch_out','work_hours','late_minutes','overtime_minutes','half_day') and rec.get(k)!=newdoc.get(k)}
    if not actual: raise HTTPException(422,'request changes nothing')
    hist={'at':now_utc(),'by':body.regularized_by,'reason':body.reason,'changes':actual}
    # Match the exact history snapshot. Legacy records may not have a history field at all.
    history_guard = {'history': rec['history']} if 'history' in rec else {'history': {'$exists': False}}
    updated=db.attendance_logs.find_one_and_update({'_id':rec['_id'], **history_guard},{'$set':{k:newdoc[k] for k in ('status','punch_in','punch_out','work_hours','late_minutes','overtime_minutes','half_day')},'$push':{'history':hist}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,'record changed concurrently; retry')
    return public_doc(updated)

@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code:str,month:str):
    first,last=valid_month(month); emp=db.employees.find_one({'emp_code':emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    start=max(first,date.fromisoformat(emp['joined_on'])); wd=working_days(start,last) if start<=last else 0
    pipeline=[{'$match':{'emp_code':emp_code,'date':{'$gte':first.isoformat(),'$lte':last.isoformat()}}},{'$addFields':{'_weekday':{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}}},'_present':present_expr()}},{'$group':{'_id':None,'present_days':{'$sum':{'$cond':[{'$and':[{'$in':['$status',list(PRESENCE)]},{'$in':['$_weekday',[2,3,4,5,6]]}]},'$_present',0]}},'leave_days':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'total_overtime_minutes':{'$sum':{'$ifNull':['$overtime_minutes',0]}}}}]
    a=list(db.attendance_logs.aggregate(pipeline, hint='attendance_employee_date_status')); r=a[0] if a else {}
    present=round_half(r.get('present_days',0)); return {'emp_code':emp_code,'month':month,'working_days':wd,'present_days':present,'leave_days':r.get('leave_days',0),'late_count':r.get('late_count',0),'total_late_minutes':r.get('total_late_minutes',0),'total_overtime_minutes':r.get('total_overtime_minutes',0),'attendance_pct':round_half(present/wd*100,4) if wd else None}

@app.get('/analytics/departments/summary')
def department_summary(month:str,department:Optional[str]=None):
    first,last=valid_month(month); match={'joined_on':{'$lte':last.isoformat()}}
    if department is not None: match['department']=department
    lookup_pipeline = [
        {'$match': {'$expr': {'$and': [
            {'$eq': ['$emp_code', '$$code']}, {'$gte': ['$date', first.isoformat()]}, {'$lte': ['$date', last.isoformat()]}
        ]}}},
        {'$addFields': {
            '_weekday': {'$dayOfWeek': {'$dateFromString': {'dateString': '$date'}}},
            '_present': present_expr()
        }},
        {'$group': {
            '_id': None,
            'present_days': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', list(PRESENCE)]}, {'$in': ['$_weekday', [2,3,4,5,6]]}]}, '$_present', 0]}},
            'work_hours_sum': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', list(PRESENCE)]}, {'$ne': ['$work_hours', None]}]}, '$work_hours', 0]}},
            'work_hours_count': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', list(PRESENCE)]}, {'$ne': ['$work_hours', None]}]}, 1, 0]}},
            'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}},
            'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}},
            'leave_count': {'$sum': {'$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]}},
            'on_duty_count': {'$sum': {'$cond': [{'$eq': ['$status', 'ON_DUTY']}, 1, 0]}}
        }}
    ]
    pipeline = [
        {'$match': match},
        {'$lookup': {'from': 'attendance_logs', 'let': {'code': '$emp_code'}, 'pipeline': lookup_pipeline, 'as': 'agg'}},
        {'$unwind': {'path': '$agg', 'preserveNullAndEmptyArrays': True}},
        {'$group': {
            '_id': '$department', 'headcount': {'$sum': 1},
            'present_days': {'$sum': {'$ifNull': ['$agg.present_days', 0]}},
            'hours_sum': {'$sum': {'$ifNull': ['$agg.work_hours_sum', 0]}},
            'hours_count': {'$sum': {'$ifNull': ['$agg.work_hours_count', 0]}},
            'late_count': {'$sum': {'$ifNull': ['$agg.late_count', 0]}},
            'total_late_minutes': {'$sum': {'$ifNull': ['$agg.total_late_minutes', 0]}},
            'leave_count': {'$sum': {'$ifNull': ['$agg.leave_count', 0]}},
            'on_duty_count': {'$sum': {'$ifNull': ['$agg.on_duty_count', 0]}}
        }},
        {'$project': {
            '_id': 0, 'department': '$_id', 'headcount': 1,
            'present_days': 1,
            'avg_work_hours': {'$cond': [{'$gt': ['$hours_count', 0]}, {'$divide': ['$hours_sum', '$hours_count']}, None]},
            'late_count': 1, 'total_late_minutes': 1, 'leave_count': 1, 'on_duty_count': 1
        }},
        {'$sort': {'department': 1}}
    ]
    items = list(db.employees.aggregate(pipeline, hint='employee_joined_on'))
    for item in items:
        item['present_days'] = round_half(item.get('present_days', 0), 2)
        if item.get('avg_work_hours') is not None:
            item['avg_work_hours'] = round_half(item['avg_work_hours'], 2)
    return {'month':month,'items':items}

@app.get('/analytics/leaderboard/late')
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    first,last=valid_month(month)
    pipeline=[{'$match':{'date':{'$gte':first.isoformat(),'$lte':last.isoformat()}}},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}},{'$match':{'total_late_minutes':{'$gt':0}}},{'$lookup':{'from':'employees','localField':'_id','foreignField':'emp_code','as':'employee'}},{'$unwind':'$employee'}]
    if department is not None:pipeline.append({'$match':{'employee.department':department}})
    pipeline += [{'$setWindowFields':{'sortBy':{'total_late_minutes':-1},'output':{'rank':{'$rank':{}}}}},{'$match':{'rank':{'$lte':limit}}},{'$sort':{'total_late_minutes':-1,'_id':1}},{'$project':{'_id':0,'rank':1,'emp_code':'$_id','name':'$employee.name','department':'$employee.department','total_late_minutes':1,'late_count':1}}]
    return {'month':month,'items':list(db.attendance_logs.aggregate(pipeline, hint='attendance_date_employee'))}

@app.get('/analytics/departments/{department}/trend')
def department_trend(department:str,from_:str=Query(alias='from'),to:str=Query(...)):
    start=valid_date(from_); end=valid_date(to)
    if end<start or (end-start).days+1>92: raise HTTPException(422,'invalid date range (maximum 92 days)')
    if not db.employees.find_one({'department':department}): raise HTTPException(404,'department not found')
    # Densify creates missing calendar rows in MongoDB; headcount is looked up by join date.
    start_dt=datetime.combine(start,time(),UTC); end_dt=datetime.combine(end,time(),UTC)
    pipeline=[{'$match':{'date':{'$gte':start.isoformat(),'$lte':end.isoformat()}}},{'$lookup':{'from':'employees','localField':'emp_code','foreignField':'emp_code','as':'employee'}},{'$unwind':'$employee'},{'$match':{'employee.department':department}},{'$group':{'_id':'$date','present_count':{'$sum':present_expr()},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}},{'$project':{'_id':0,'day':{'$dateFromString':{'dateString':'$_id'}},'present_count':1,'late_count':1}},{'$densify':{'field':'day','range':{'step':1,'unit':'day','bounds':[start_dt,end_dt+timedelta(days=1)]}}},{'$set':{'date':{'$dateToString':{'date':'$day','format':'%Y-%m-%d'}},'present_count':{'$ifNull':['$present_count',0]},'late_count':{'$ifNull':['$late_count',0]},'is_working_day':{'$in':[{'$dayOfWeek':'$day'},[2,3,4,5,6]]}}},{'$lookup':{'from':'employees','let':{'day':'$date'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$department',department]},{'$lte':['$joined_on','$$day']}]}}},{'$count':'n'}],'as':'hc'}},{'$set':{'headcount':{'$ifNull':[{'$arrayElemAt':['$hc.n',0]},0]} }},{'$set':{'attendance_rate':{'$cond':[{'$and':['$is_working_day',{'$gt':['$headcount',0]}]},mongo_round_half_up({'$divide':['$present_count','$headcount']}, 4),None]}}},{'$setWindowFields':{'sortBy':{'day':1},'output':{'moving_avg_7d':{'$avg':'$attendance_rate','window':{'documents':[-6,0]}}}}},{'$project':{'_id':0,'date':1,'is_working_day':1,'headcount':1,'present_count':1,'late_count':1,'attendance_rate':1,'moving_avg_7d':1}},{'$sort':{'date':1}}]
    items=list(db.attendance_logs.aggregate(pipeline, hint='attendance_date_employee'))
    # If range contains no logs, $densify has no input; seed a calendar spine in MongoDB via $documents.
    if not items:
        spine=[{'$documents':[{'day':start_dt}]},{'$densify':{'field':'day','range':{'step':1,'unit':'day','bounds':[start_dt,end_dt+timedelta(days=1)]}}},{'$set':{'date':{'$dateToString':{'date':'$day','format':'%Y-%m-%d'}},'is_working_day':{'$in':[{'$dayOfWeek':'$day'},[2,3,4,5,6]]},'present_count':0,'late_count':0}},{'$lookup':{'from':'employees','let':{'day':'$date'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$department',department]},{'$lte':['$joined_on','$$day']}]}}},{'$count':'n'}],'as':'hc'}},{'$set':{'headcount':{'$ifNull':[{'$arrayElemAt':['$hc.n',0]},0]} }},{'$set':{'attendance_rate':{'$cond':[{'$and':['$is_working_day',{'$gt':['$headcount',0]}]},0,None]}}},{'$setWindowFields':{'sortBy':{'day':1},'output':{'moving_avg_7d':{'$avg':'$attendance_rate','window':{'documents':[-6,0]}}}}},{'$project':{'_id':0,'date':1,'is_working_day':1,'headcount':1,'present_count':1,'late_count':1,'attendance_rate':1,'moving_avg_7d':1}}]
        items=list(db.command({'aggregate':1,'pipeline':spine,'cursor':{},'allowDiskUse':True})['cursor']['firstBatch'])
    for row in items:
        for k in ('attendance_rate','moving_avg_7d'):
            if row.get(k) is not None: row[k]=round_half(row[k],4)
        row['present_count']=round_half(row.get('present_count',0))
    return {'department':department,'items':items}

@app.get('/admin/explain/{endpoint}')
def explain(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[Literal['PRESENT','WFH','ON_DUTY','ABSENT','LEAVE']]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),from_:Optional[str]=Query(None,alias='from'),to:Optional[str]=None):
    """Explain the same main read operation used by each analytics/list endpoint."""
    allowed={'attendance_list','employee_monthly','department_summary','late_leaderboard','department_trend'}
    if endpoint not in allowed: raise HTTPException(422,'unknown endpoint')
    if endpoint == 'attendance_list':
        if date_from: valid_date(date_from)
        if date_to: valid_date(date_to)
        if date_from and date_to and date_from > date_to: raise HTTPException(422,'date_from must be <= date_to')
        q={}
        if emp_code: q['emp_code']=emp_code
        if status: q['status']=status
        if date_from or date_to:
            q['date']={}
            if date_from: q['date']['$gte']=date_from
            if date_to: q['date']['$lte']=date_to
        command={'find':'attendance_logs','filter':q,'sort':{'date':-1,'emp_code':1},'skip':(page-1)*page_size,'limit':page_size,'hint':'attendance_date_employee'}
        exp=db.command('explain',command,verbosity='executionStats'); coll='attendance_logs'
    elif endpoint == 'employee_monthly':
        if not emp_code or not month: raise HTTPException(422,'emp_code and month are required')
        first,last=valid_month(month)
        pipe=[{'$match':{'emp_code':emp_code,'date':{'$gte':first.isoformat(),'$lte':last.isoformat()}}},{'$addFields':{'_weekday':{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}},},'_present':present_expr()}},{'$group':{'_id':None,'present_days':{'$sum':{'$cond':[{'$and':[{'$in':['$status',list(PRESENCE)]},{'$in':['$_weekday',[2,3,4,5,6]]}]},'$_present',0]}},'leave_days':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'total_overtime_minutes':{'$sum':{'$ifNull':['$overtime_minutes',0]}}}}]
        exp=db.command('explain',{'aggregate':'attendance_logs','pipeline':pipe,'cursor':{},'allowDiskUse':True,'hint':'attendance_employee_date_status'},verbosity='executionStats'); coll='attendance_logs'
    elif endpoint == 'department_summary':
        if not month: raise HTTPException(422,'month is required')
        first,last=valid_month(month)
        emp_match={'joined_on':{'$lte':last.isoformat()}}
        if department is not None: emp_match['department']=department
        logs=[{'$match':{'$expr':{'$and':[{'$eq':['$emp_code','$$code']},{'$gte':['$date',first.isoformat()]},{'$lte':['$date',last.isoformat()]}]}}},{'$addFields':{'_weekday':{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}},},'_present':present_expr()}},{'$group':{'_id':None,'present_days':{'$sum':{'$cond':[{'$and':[{'$in':['$status',list(PRESENCE)]},{'$in':['$_weekday',[2,3,4,5,6]]}]},'$_present',0]}},'work_hours_sum':{'$sum':{'$cond':[{'$and':[{'$in':['$status',list(PRESENCE)]},{'$ne':['$work_hours',None]}]},'$work_hours',0]}},'work_hours_count':{'$sum':{'$cond':[{'$and':[{'$in':['$status',list(PRESENCE)]},{'$ne':['$work_hours',None]}]},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'leave_count':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},'on_duty_count':{'$sum':{'$cond':[{'$eq':['$status','ON_DUTY']},1,0]}}}}]
        pipe=[{'$match':emp_match},{'$lookup':{'from':'attendance_logs','let':{'code':'$emp_code'},'pipeline':logs,'as':'agg'}},{'$unwind':{'path':'$agg','preserveNullAndEmptyArrays':True}},{'$group':{'_id':'$department','headcount':{'$sum':1},'present_days':{'$sum':{'$ifNull':['$agg.present_days',0]}},'hours_sum':{'$sum':{'$ifNull':['$agg.work_hours_sum',0]}},'hours_count':{'$sum':{'$ifNull':['$agg.work_hours_count',0]}},'late_count':{'$sum':{'$ifNull':['$agg.late_count',0]}},'total_late_minutes':{'$sum':{'$ifNull':['$agg.total_late_minutes',0]}},'leave_count':{'$sum':{'$ifNull':['$agg.leave_count',0]}},'on_duty_count':{'$sum':{'$ifNull':['$agg.on_duty_count',0]}}}},{'$project':{'_id':0,'department':'$_id','headcount':1,'present_days':1,'avg_work_hours':{'$cond':[{'$gt':['$hours_count',0]},{'$divide':['$hours_sum','$hours_count']},None]},'late_count':1,'total_late_minutes':1,'leave_count':1,'on_duty_count':1}},{'$sort':{'department':1}}]
        exp=db.command('explain',{'aggregate':'employees','pipeline':pipe,'cursor':{},'allowDiskUse':True,'hint':'employee_joined_on'},verbosity='executionStats'); coll='employees'
    elif endpoint == 'late_leaderboard':
        if not month: raise HTTPException(422,'month is required')
        first,last=valid_month(month)
        pipe=[{'$match':{'date':{'$gte':first.isoformat(),'$lte':last.isoformat()}}},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}},{'$match':{'total_late_minutes':{'$gt':0}}},{'$lookup':{'from':'employees','localField':'_id','foreignField':'emp_code','as':'employee'}},{'$unwind':'$employee'}]
        if department is not None: pipe.append({'$match':{'employee.department':department}})
        pipe += [{'$setWindowFields':{'sortBy':{'total_late_minutes':-1},'output':{'rank':{'$rank':{}}}}},{'$match':{'rank':{'$lte':limit}}},{'$sort':{'total_late_minutes':-1,'_id':1}},{'$project':{'_id':0,'rank':1,'emp_code':'$_id','name':'$employee.name','department':'$employee.department','total_late_minutes':1,'late_count':1}}]
        exp=db.command('explain',{'aggregate':'attendance_logs','pipeline':pipe,'cursor':{},'allowDiskUse':True,'hint':'attendance_date_employee'},verbosity='executionStats'); coll='attendance_logs'
    else:
        if not department or not from_ or not to: raise HTTPException(422,'department, from and to are required')
        start=valid_date(from_); end=valid_date(to)
        if end < start or (end-start).days+1 > 92: raise HTTPException(422,'invalid date range (maximum 92 days)')
        start_dt=datetime.combine(start,time(),UTC); end_dt=datetime.combine(end,time(),UTC)
        pipe=[{'$match':{'date':{'$gte':start.isoformat(),'$lte':end.isoformat()}}},{'$lookup':{'from':'employees','localField':'emp_code','foreignField':'emp_code','as':'employee'}},{'$unwind':'$employee'},{'$match':{'employee.department':department}},{'$group':{'_id':'$date','present_count':{'$sum':present_expr()},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}},{'$project':{'_id':0,'day':{'$dateFromString':{'dateString':'$_id'}},'present_count':1,'late_count':1}},{'$densify':{'field':'day','range':{'step':1,'unit':'day','bounds':[start_dt,end_dt+timedelta(days=1)]}}},{'$set':{'date':{'$dateToString':{'date':'$day','format':'%Y-%m-%d'}},'present_count':{'$ifNull':['$present_count',0]},'late_count':{'$ifNull':['$late_count',0]},'is_working_day':{'$in':[{'$dayOfWeek':'$day'},[2,3,4,5,6]]}}},{'$lookup':{'from':'employees','let':{'day':'$date'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$department',department]},{'$lte':['$joined_on','$$day']}]}}},{'$count':'n'}],'as':'hc'}},{'$set':{'headcount':{'$ifNull':[{'$arrayElemAt':['$hc.n',0]},0]} }},{'$set':{'attendance_rate':{'$cond':[{'$and':['$is_working_day',{'$gt':['$headcount',0]}]},mongo_round_half_up({'$divide':['$present_count','$headcount']},4),None]}}},{'$setWindowFields':{'sortBy':{'day':1},'output':{'moving_avg_7d':{'$avg':'$attendance_rate','window':{'documents':[-6,0]}}}}},{'$project':{'_id':0,'date':1,'is_working_day':1,'headcount':1,'present_count':1,'late_count':1,'attendance_rate':1,'moving_avg_7d':1}},{'$sort':{'date':1}}]
        exp=db.command('explain',{'aggregate':'attendance_logs','pipeline':pipe,'cursor':{},'allowDiskUse':True,'hint':'attendance_date_employee'},verbosity='executionStats'); coll='attendance_logs'
    return {'endpoint':endpoint,'collection':coll,'explain':clean_mongo(exp)}
