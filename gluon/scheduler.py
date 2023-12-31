#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
| This file is part of the web2py Web Framework
| Copyrighted by Massimo Di Pierro <mdipierro@cs.depaul.edu>
| License: LGPLv3 (http://www.gnu.org/licenses/lgpl.html)

Background processes made simple
---------------------------------
"""
from __future__ import print_function

import os
import re
import time
import multiprocessing
import sys
import threading
import traceback
import signal
import socket
import datetime
import logging
import optparse
import tempfile
import types
from functools import reduce
from json import loads, dumps
from gluon import DAL, Field, IS_NOT_EMPTY, IS_IN_SET, IS_NOT_IN_DB, IS_EMPTY_OR
from gluon import IS_INT_IN_RANGE, IS_DATETIME, IS_IN_DB
from gluon.utils import web2py_uuid
from gluon._compat import Queue, long, iteritems, PY2
from gluon.storage import Storage

USAGE = """
## Example

For any existing app

Create File: app/models/scheduler.py ======
from gluon.scheduler import Scheduler

def demo1(*args,**vars):
    print('you passed args=%s and vars=%s' % (args, vars))
    return 'done!'

def demo2():
    1/0

scheduler = Scheduler(db,dict(demo1=demo1,demo2=demo2))
## run worker nodes with:

   cd web2py
   python web2py.py -K myapp
or
   python gluon/scheduler.py -u sqlite://storage.sqlite \
                             -f applications/myapp/databases/ \
                             -t mytasks.py
(-h for info)
python scheduler.py -h

## schedule jobs using
http://127.0.0.1:8181/myapp/appadmin/insert/db/scheduler_task

## monitor scheduled jobs
http://127.0.0.1:8181/myapp/appadmin/select/db?query=db.scheduler_task.id>0

## view completed jobs
http://127.0.0.1:8181/myapp/appadmin/select/db?query=db.scheduler_run.id>0

## view workers
http://127.0.0.1:8181/myapp/appadmin/select/db?query=db.scheduler_worker.id>0

"""

path = os.getcwd()

if 'WEB2PY_PATH' not in os.environ:
    os.environ['WEB2PY_PATH'] = path

IDENTIFIER = "%s#%s" % (socket.gethostname(), os.getpid())

logger = logging.getLogger('web2py.scheduler.%s' % IDENTIFIER)

QUEUED = 'QUEUED'
ASSIGNED = 'ASSIGNED'
RUNNING = 'RUNNING'
COMPLETED = 'COMPLETED'
FAILED = 'FAILED'
TIMEOUT = 'TIMEOUT'
STOPPED = 'STOPPED'
ACTIVE = 'ACTIVE'
TERMINATE = 'TERMINATE'
DISABLED = 'DISABLED'
KILL = 'KILL'
PICK = 'PICK'
STOP_TASK = 'STOP_TASK'
EXPIRED = 'EXPIRED'
SECONDS = 1
HEARTBEAT = 3 * SECONDS
MAXHIBERNATION = 10
CLEAROUT = '!clear!'

CALLABLETYPES = (types.LambdaType, types.FunctionType,
                 types.BuiltinFunctionType,
                 types.MethodType, types.BuiltinMethodType)


class Task(object):
    """Defines a "task" object that gets passed from the main thread to the
    executor's one
    """
    def __init__(self, app, function, timeout, args='[]', vars='{}', **kwargs):
        logger.debug(' new task allocated: %s.%s', app, function)
        self.app = app
        self.function = function
        self.timeout = timeout
        self.args = args  # json
        self.vars = vars  # json
        self.__dict__.update(kwargs)

    def __str__(self):
        return '<Task: %s>' % self.function


class TaskReport(object):
    """Defines a "task report" object that gets passed from the executor's
    thread to the main one
    """
    def __init__(self, status, result=None, output=None, tb=None):
        logger.debug('    new task report: %s', status)
        if tb:
            logger.debug('   traceback: %s', tb)
        else:
            logger.debug('   result: %s', result)
        self.status = status
        self.result = result
        self.output = output
        self.tb = tb

    def __str__(self):
        return '<TaskReport: %s>' % self.status


class JobGraph(object):
    """Experimental: dependencies amongs tasks."""

    def __init__(self, db, job_name):
        self.job_name = job_name or 'job_0'
        self.db = db

    def add_deps(self, task_parent, task_child):
        """Create a dependency between task_parent and task_child."""
        self.db.scheduler_task_deps.insert(task_parent=task_parent,
                                           task_child=task_child,
                                           job_name=self.job_name)

    def validate(self, job_name=None):
        """Validate if all tasks job_name can be completed.

        Checks if there are no mutual dependencies among tasks.
        Commits at the end if successfull, or it rollbacks the entire
        transaction. Handle with care!
        """
        db = self.db
        sd = db.scheduler_task_deps
        if job_name:
            q = sd.job_name == job_name
        else:
            q = sd.id > 0

        edges = db(q).select()
        nested_dict = {}
        for row in edges:
            k = row.task_parent
            if k in nested_dict:
                nested_dict[k].add(row.task_child)
            else:
                nested_dict[k] = set((row.task_child,))
        try:
            rtn = []
            for k, v in nested_dict.items():
                v.discard(k)  # Ignore self dependencies
            extra_items_in_deps = reduce(set.union, nested_dict.values()) - set(nested_dict.keys())
            nested_dict.update(dict((item, set()) for item in extra_items_in_deps))
            while True:
                ordered = set(item for item, dep in nested_dict.items() if not dep)
                if not ordered:
                    break
                rtn.append(ordered)
                nested_dict = dict(
                    (item, (dep - ordered)) for item, dep in nested_dict.items()
                    if item not in ordered
                )
            assert not nested_dict, "A cyclic dependency exists amongst %r" % nested_dict
            db.commit()
            return rtn
        except:
            db.rollback()
            return None


class CronParser(object):

    def __init__(self, cronline, base=None):
        self.cronline = cronline
        self.sched = base or datetime.datetime.now()
        self.task = None

    @staticmethod
    def _rangetolist(s, period='min'):
        retval = []
        if s.startswith('*'):
            if period == 'min':
                s = s.replace('*', '0-59', 1)
            elif period == 'hr':
                s = s.replace('*', '0-23', 1)
            elif period == 'dom':
                s = s.replace('*', '1-31', 1)
            elif period == 'mon':
                s = s.replace('*', '1-12', 1)
            elif period == 'dow':
                s = s.replace('*', '0-6', 1)
        m = re.compile(r'(\d+)-(\d+)/(\d+)')
        match = m.match(s)
        if match:
            min_, max_ = int(match.group(1)), int(match.group(2)) + 1
            step_ = int(match.group(3))
        else:
            m = re.compile(r'(\d+)/(\d+)')
            ranges_max = {'min': 59, 'hr': 23, 'mon': 12, 'dom': 31, 'dow': 7}
            match = m.match(s)
            if match:
                min_, max_ = int(match.group(1)), ranges_max[period] + 1
                step_ = int(match.group(2))
        if match:
            for i in range(min_, max_, step_):
                retval.append(i)
        return retval

    @staticmethod
    def _sanitycheck(values, period):
        if period == 'min':
            check = all(0 <= i <= 59 for i in values)
        elif period == 'hr':
            check = all(0 <= i <= 23 for i in values)
        elif period == 'dom':
            domrange = list(range(1, 32)) + ['l']
            check = all(i in domrange for i in values)
        elif period == 'mon':
            check = all(1 <= i <= 12 for i in values)
        elif period == 'dow':
            check = all(0 <= i <= 7 for i in values)
        return check

    def _parse(self):
        line = self.cronline.lower()
        task = {}
        if line.startswith('@yearly'):
            line = line.replace('@yearly', '0 0 1 1 *')
        elif line.startswith('@annually'):
            line = line.replace('@annually', '0 0 1 1 *')
        elif line.startswith('@monthly'):
            line = line.replace('@monthly', '0 0 1 * *')
        elif line.startswith('@weekly'):
            line = line.replace('@weekly', '0 0 * * 0')
        elif line.startswith('@daily'):
            line = line.replace('@daily', '0 0 * * *')
        elif line.startswith('@midnight'):
            line = line.replace('@midnight', '0 0 * * *')
        elif line.startswith('@hourly'):
            line = line.replace('@hourly', '0 * * * *')
        params = line.strip().split()
        if len(params) < 5:
            raise ValueError('Invalid cron line (too short)')
        elif len(params) > 5:
            raise ValueError('Invalid cron line (too long)')
        daysofweek = {'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4,
                      'fri': 5, 'sat': 6}
        monthsofyear = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5,
                        'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10,
                        'nov': 11, 'dec': 12, 'l': 'l'}
        for (s, i) in zip(params[:5], ['min', 'hr', 'dom', 'mon', 'dow']):
            if s not in [None, '*']:
                task[i] = []
                vals = s.split(',')
                for val in vals:
                    if i == 'dow':
                        refdict = daysofweek
                    elif i == 'mon':
                        refdict = monthsofyear
                    if i in ('dow', 'mon') and '-' in val and '/' not in val:
                        isnum = val.split('-')[0].isdigit()
                        if isnum:
                            val = '%s/1' % val
                        else:
                            val = '-'.join([str(refdict[v])
                                           for v in val.split('-')])
                    if val != '-1' and '-' in val and '/' not in val:
                        val = '%s/1' % val
                    if '/' in val:
                        task[i] += self._rangetolist(val, i)
                    elif val.isdigit() or val == '-1':
                        task[i].append(int(val))
                    elif i in ('dow', 'mon'):
                        if val in refdict:
                            task[i].append(refdict[val])
                    elif i == 'dom' and val == 'l':
                        task[i].append(val)
                if not task[i]:
                    raise ValueError('Invalid cron value (%s)' % s)
                if not self._sanitycheck(task[i], i):
                    raise ValueError('Invalid cron value (%s)' % s)
                task[i] = sorted(task[i])
        self.task = task

    @staticmethod
    def _get_next_dow(sched, task):
        task_dow = [a % 7 for a in task['dow']]
        while sched.isoweekday() % 7 not in task_dow:
            sched += datetime.timedelta(days=1)
        return sched

    @staticmethod
    def _get_next_dom(sched, task):
        if task['dom'] == ['l']:
            last_feb = 29 if sched.year % 4 == 0 else 28
            lastdayofmonth = [
                31, last_feb, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31
            ]
            task_dom = [lastdayofmonth[sched.month - 1]]
        else:
            task_dom = task['dom']
        while sched.day not in task_dom:
            sched += datetime.timedelta(days=1)
        return sched

    @staticmethod
    def _get_next_mon(sched, task):
        while sched.month not in task['mon']:
            if sched.month < 12:
                sched = sched.replace(month=sched.month + 1)
            else:
                sched = sched.replace(month=1, year=sched.year + 1)
        return sched

    @staticmethod
    def _getnext_hhmm(sched, task, add_to=True):
        if add_to:
            sched += datetime.timedelta(minutes=1)
        if 'min' in task:
            while sched.minute not in task['min']:
                sched += datetime.timedelta(minutes=1)
        if 'hr' in task and sched.hour not in task['hr']:
            while sched.hour not in task['hr']:
                sched += datetime.timedelta(hours=1)
        return sched

    def _getnext_date(self, sched, task):
        if 'dow' in task and 'dom' in task:
            dow = self._get_next_dow(sched, task)
            dom = self._get_next_dom(sched, task)
            sched = min(dow, dom)
        elif 'dow' in task:
            sched = self._get_next_dow(sched, task)
        elif 'dom' in task:
            sched = self._get_next_dom(sched, task)
        if 'mon' in task:
            sched = self._get_next_mon(sched, task)
        return sched.replace(hour=0, minute=0)

    def get_next(self):
        """Get next date according to specs."""
        if not self.task:
            self._parse()
        task = self.task
        sched = self.sched
        x = 0
        while x < 1000:  # avoid potential max recursions
            x += 1
            try:
                next_date = self._getnext_date(sched, task)
            except (ValueError, OverflowError) as e:
                raise ValueError('Invalid cron expression (%s)' % e)
            if next_date.date() > self.sched.date():
                # we rolled date, check for valid hhmm
                sched = self._getnext_hhmm(next_date, task, False)
                break
            else:
                # same date, get next hhmm
                sched_time = self._getnext_hhmm(sched, task, True)
                if sched_time.date() > sched.date():
                    # we rolled date again :(
                    sched = sched_time
                else:
                    sched = sched_time
                    break
        else:
            raise ValueError('Potential bug found, please submit your '
                             'cron expression to the authors')
        self.sched = sched
        return sched

    def __iter__(self):
        """Support iteration."""
        return self

    __next__ = next = get_next

# the two functions below deal with simplejson decoding as unicode, esp for the dict decode
# and subsequent usage as function Keyword arguments unicode variable names won't work!
# borrowed from http://stackoverflow.com/questions/956867/


def _decode_list(lst):
    if not PY2:
        return lst
    newlist = []
    for i in lst:
        if isinstance(i, unicode):
            i = i.encode('utf-8')
        elif isinstance(i, list):
            i = _decode_list(i)
        newlist.append(i)
    return newlist


def _decode_dict(dct):
    if not PY2:
        return dct
    newdict = {}
    for k, v in iteritems(dct):
        if isinstance(k, unicode):
            k = k.encode('utf-8')
        if isinstance(v, unicode):
            v = v.encode('utf-8')
        elif isinstance(v, list):
            v = _decode_list(v)
        newdict[k] = v
    return newdict


def executor(queue, task, out):
    """The function used to execute tasks in the background process."""
    logger.debug('    task started')

    class LogOutput(object):
        """Facility to log output at intervals."""

        def __init__(self, out_queue):
            self.out_queue = out_queue
            self.stdout = sys.stdout
            sys.stdout = self

        def __del__(self):
            sys.stdout = self.stdout

        def flush(self):
            pass

        def write(self, data):
            self.out_queue.put(data)

    W2P_TASK = Storage({
                       'id': task.task_id,
                       'uuid': task.uuid,
                       'run_id': task.run_id
                       })
    stdout = LogOutput(out)
    try:
        if task.app:
            os.chdir(os.environ['WEB2PY_PATH'])
            from gluon.shell import env, parse_path_info
            from gluon import current
            level = logging.getLogger().getEffectiveLevel()
            logging.getLogger().setLevel(logging.WARN)
            # Get controller-specific subdirectory if task.app is of
            # form 'app/controller'
            (a, c, f) = parse_path_info(task.app)
            _env = env(a=a, c=c, import_models=True,
                       extra_request={'is_scheduler': True})
            logging.getLogger().setLevel(level)
            f = task.function
            functions = current._scheduler.tasks
            if not functions:
                # look into env
                _function = _env.get(f)
            else:
                _function = functions.get(f)
            if not isinstance(_function, CALLABLETYPES):
                raise NameError(
                    "name '%s' not found in scheduler's environment" % f)
            # Inject W2P_TASK into environment
            _env.update({'W2P_TASK': W2P_TASK})
            # Inject W2P_TASK into current
            from gluon import current
            current.W2P_TASK = W2P_TASK
            globals().update(_env)
            args = _decode_list(loads(task.args))
            vars = loads(task.vars, object_hook=_decode_dict)
            result = dumps(_function(*args, **vars))
        else:
            # for testing purpose only
            result = eval(task.function)(
                *loads(task.args, object_hook=_decode_dict),
                **loads(task.vars, object_hook=_decode_dict))
        if len(result) >= 1024:
            fd, temp_path = tempfile.mkstemp(suffix='.w2p_sched')
            with os.fdopen(fd, 'w') as f:
                f.write(result)
            result = 'w2p_special:%s' % temp_path
        queue.put(TaskReport('COMPLETED', result=result))
    except BaseException as e:
        tb = traceback.format_exc()
        queue.put(TaskReport('FAILED', tb=tb))
    del stdout


class MetaScheduler(threading.Thread):
    """Base class documenting scheduler's base methods."""

    def __init__(self):
        threading.Thread.__init__(self)
        self.process = None     # the background process
        self.have_heartbeat = True   # set to False to kill
        self.empty_runs = 0

    def async(self, task):
        """Start the background process.

        Args:
            task : a `Task` object

        Returns:
            tuple: containing::

            ('ok',result,output)
            ('error',exception,None)
            ('timeout',None,None)
            ('terminated',None,None)

        """
        db = self.db
        sr = db.scheduler_run
        out = multiprocessing.Queue()
        queue = multiprocessing.Queue(maxsize=1)
        p = multiprocessing.Process(target=executor, args=(queue, task, out))
        self.process = p
        logger.debug('   task starting')
        p.start()

        task_output = ""
        tout = ""

        try:
            if task.sync_output > 0:
                run_timeout = task.sync_output
            else:
                run_timeout = task.timeout

            start = time.time()

            while p.is_alive() and (not task.timeout or time.time() - start < task.timeout):
                if tout:
                    try:
                        logger.debug(' partial output saved')
                        db(sr.id == task.run_id).update(run_output=task_output)
                        db.commit()
                    except:
                        pass
                p.join(timeout=run_timeout)
                tout = ""
                while not out.empty():
                    tout += out.get()
                if tout:
                    logger.debug(' partial output: "%s"', str(tout))
                    if CLEAROUT in tout:
                        task_output = tout[
                            tout.rfind(CLEAROUT) + len(CLEAROUT):]
                    else:
                        task_output += tout
        except:
            p.terminate()
            p.join()
            logger.debug('    task stopped by general exception')
            tr = TaskReport(STOPPED)
        else:
            if p.is_alive():
                p.terminate()
                logger.debug('    task timeout')
                try:
                    # we try to get a traceback here
                    tr = queue.get(timeout=2)
                    tr.status = TIMEOUT
                    tr.output = task_output
                except Queue.Empty:
                    tr = TaskReport(TIMEOUT)
            elif queue.empty():
                logger.debug('    task stopped')
                tr = TaskReport(STOPPED)
            else:
                logger.debug('  task completed or failed')
                tr = queue.get()
        result = tr.result
        if result and result.startswith('w2p_special'):
            temp_path = result.replace('w2p_special:', '', 1)
            with open(temp_path) as f:
                tr.result = f.read()
            os.unlink(temp_path)
        tr.output = task_output
        return tr

    def die(self):
        """Forces termination of the worker process along with any running
        task"""
        logger.info('die!')
        self.have_heartbeat = False
        self.terminate_process()

    def give_up(self):
        """Waits for any running task to be executed, then exits the worker
        process"""
        logger.info('Giving up as soon as possible!')
        self.have_heartbeat = False

    def terminate_process(self):
        """Terminate any running tasks (internal use only)"""
        try:
            self.process.terminate()
        except:
            pass  # no process to terminate

    def run(self):
        """This is executed by the main thread to send heartbeats"""
        counter = 0
        while self.have_heartbeat:
            self.send_heartbeat(counter)
            counter += 1

    def start_heartbeats(self):
        self.start()

    def send_heartbeat(self, counter):
        raise NotImplementedError

    def pop_task(self):
        """Fetches a task ready to be executed"""
        raise NotImplementedError

    def report_task(self, task, task_report):
        """Creates a task report"""
        raise NotImplementedError

    def sleep(self):
        raise NotImplementedError

    def loop(self):
        """Main loop, fetching tasks and starting executor's background
        processes"""
        raise NotImplementedError


TASK_STATUS = (QUEUED, RUNNING, COMPLETED, FAILED, TIMEOUT, STOPPED, EXPIRED)
RUN_STATUS = (RUNNING, COMPLETED, FAILED, TIMEOUT, STOPPED)
WORKER_STATUS = (ACTIVE, PICK, DISABLED, TERMINATE, KILL, STOP_TASK)

class IS_CRONLINE(object):
    """
    Validates cronline
    """
    def __init__(self, error_message=None):
        self.error_message = error_message

    def __call__(self, value):
        recur = CronParser(value, datetime.datetime.now())
        try:
            recur.get_next()
            return (value, None)
        except (KeyError, ValueError) as e:
            if not self.error_message:
                return (value, e)
            return (value, self.error_message)

class TYPE(object):
    """
    Validator that checks whether field is valid json and validates its type.
    Used for `args` and `vars` of the scheduler_task table
    """

    def __init__(self, myclass=list, parse=False):
        self.myclass = myclass
        self.parse = parse

    def __call__(self, value):
        from gluon import current
        try:
            obj = loads(value)
        except:
            return (value, current.T('invalid json'))
        else:
            if isinstance(obj, self.myclass):
                if self.parse:
                    return (obj, None)
                else:
                    return (value, None)
            else:
                return (value, current.T('Not of type: %s') % self.myclass)


class Scheduler(MetaScheduler):
    """Scheduler object

    Args:
        db: DAL connection where Scheduler will create its tables
        tasks(dict): either a dict containing name-->func or None.
            If None, functions will be searched in the environment
        migrate(bool): turn migration on/off for the Scheduler's tables
        worker_name(str): force worker_name to identify each process.
            Leave it to None to autoassign a name (hostname#pid)
        group_names(list): process tasks belonging to this group
            defaults to ['main'] if nothing gets passed
        heartbeat(int): how many seconds the worker sleeps between one
            execution and the following one. Indirectly sets how many seconds
            will pass between checks for new tasks
        max_empty_runs(int): how many loops are allowed to pass without
            processing any tasks before exiting the process. 0 to keep always
            the process alive
        discard_results(bool): Scheduler stores executions's details into the
            scheduler_run table. By default, only if there is a result the
            details are kept. Turning this to True means discarding results
            even for tasks that return something
        utc_time(bool): do all datetime calculations assuming UTC as the
            timezone. Remember to pass `start_time` and `stop_time` to tasks
            accordingly

    """

    def __init__(self, db, tasks=None, migrate=True,
                 worker_name=None, group_names=None, heartbeat=HEARTBEAT,
                 max_empty_runs=0, discard_results=False, utc_time=False):

        MetaScheduler.__init__(self)

        self.db = db
        self.db_thread = None
        self.tasks = tasks
        self.group_names = group_names or ['main']
        self.heartbeat = heartbeat
        self.worker_name = worker_name or IDENTIFIER
        self.max_empty_runs = max_empty_runs
        self.discard_results = discard_results
        self.is_a_ticker = False
        self.do_assign_tasks = False
        self.greedy = False
        self.utc_time = utc_time
        self.w_stats = Storage(
            dict(
                status=RUNNING,
                sleep=heartbeat,
                total=0,
                errors=0,
                empty_runs=0,
                queue=0,
                distribution=None,
                workers=0)
        )  # dict holding statistics

        from gluon import current
        current._scheduler = self

        self.define_tables(db, migrate=migrate)

    def __get_migrate(self, tablename, migrate=True):
        if migrate is False:
            return False
        elif migrate is True:
            return True
        elif isinstance(migrate, str):
            return "%s%s.table" % (migrate, tablename)
        return True

    def now(self):
        """Shortcut that fetches current time based on UTC preferences."""
        return self.utc_time and datetime.datetime.utcnow() or datetime.datetime.now()

    def set_requirements(self, scheduler_task):
        """Called to set defaults for lazy_tables connections."""
        from gluon import current
        if hasattr(current, 'request'):
            scheduler_task.application_name.default = '%s/%s' % (
                current.request.application, current.request.controller
            )

    def define_tables(self, db, migrate):
        """Define Scheduler tables structure."""
        from pydal.base import DEFAULT
        logger.debug('defining tables (migrate=%s)', migrate)
        now = self.now
        db.define_table(
            'scheduler_task',
            Field('application_name', requires=IS_NOT_EMPTY(),
                  default=None, writable=False),
            Field('task_name', default=None),
            Field('group_name', default='main'),
            Field('status', requires=IS_IN_SET(TASK_STATUS),
                  default=QUEUED, writable=False),
            Field('function_name',
                  requires=IS_IN_SET(sorted(self.tasks.keys()))
                  if self.tasks else DEFAULT),
            Field('uuid', length=255,
                  requires=IS_NOT_IN_DB(db, 'scheduler_task.uuid'),
                  unique=True, default=web2py_uuid),
            Field('args', 'text', default='[]', requires=TYPE(list)),
            Field('vars', 'text', default='{}', requires=TYPE(dict)),
            Field('enabled', 'boolean', default=True),
            Field('start_time', 'datetime', default=now,
                  requires=IS_DATETIME()),
            Field('next_run_time', 'datetime', default=now),
            Field('stop_time', 'datetime'),
            Field('repeats', 'integer', default=1, comment="0=unlimited",
                  requires=IS_INT_IN_RANGE(0, None)),
            Field('retry_failed', 'integer', default=0, comment="-1=unlimited",
                  requires=IS_INT_IN_RANGE(-1, None)),
            Field('period', 'integer', default=60, comment='seconds',
                  requires=IS_INT_IN_RANGE(0, None)),
            Field('prevent_drift', 'boolean', default=False,
                  comment='Exact start_times between runs'),
            Field('cronline', default=None,
                  comment='Discard "period", use this cron expr instead',
                  requires=IS_EMPTY_OR(IS_CRONLINE())),
            Field('timeout', 'integer', default=60, comment='seconds',
                  requires=IS_INT_IN_RANGE(1, None)),
            Field('sync_output', 'integer', default=0,
                  comment="update output every n sec: 0=never",
                  requires=IS_INT_IN_RANGE(0, None)),
            Field('times_run', 'integer', default=0, writable=False),
            Field('times_failed', 'integer', default=0, writable=False),
            Field('last_run_time', 'datetime', writable=False, readable=False),
            Field('assigned_worker_name', default='', writable=False),
            on_define=self.set_requirements,
            migrate=self.__get_migrate('scheduler_task', migrate),
            format='(%(id)s) %(task_name)s')

        db.define_table(
            'scheduler_run',
            Field('task_id', 'reference scheduler_task'),
            Field('status', requires=IS_IN_SET(RUN_STATUS)),
            Field('start_time', 'datetime'),
            Field('stop_time', 'datetime'),
            Field('run_output', 'text'),
            Field('run_result', 'text'),
            Field('traceback', 'text'),
            Field('worker_name', default=self.worker_name),
            migrate=self.__get_migrate('scheduler_run', migrate)
        )

        db.define_table(
            'scheduler_worker',
            Field('worker_name', length=255, unique=True),
            Field('first_heartbeat', 'datetime'),
            Field('last_heartbeat', 'datetime'),
            Field('status', requires=IS_IN_SET(WORKER_STATUS)),
            Field('is_ticker', 'boolean', default=False, writable=False),
            Field('group_names', 'list:string', default=self.group_names),
            Field('worker_stats', 'json'),
            migrate=self.__get_migrate('scheduler_worker', migrate)
        )

        db.define_table(
            'scheduler_task_deps',
            Field('job_name', default='job_0'),
            Field('task_parent', 'integer',
                  requires=IS_IN_DB(db, 'scheduler_task.id', '%(task_name)s')
                  ),
            Field('task_child', 'reference scheduler_task'),
            Field('can_visit', 'boolean', default=False),
            migrate=self.__get_migrate('scheduler_task_deps', migrate)
        )

        if migrate is not False:
            db.commit()

    def loop(self, worker_name=None):
        """Main loop.

        This works basically as a neverending loop that:

        - checks if the worker is ready to process tasks (is not DISABLED)
        - pops a task from the queue
        - if there is a task:

          - spawns the executor background process
          - waits for the process to be finished
          - sleeps `heartbeat` seconds
        - if there is not a task:

          - checks for max_empty_runs
          - sleeps `heartbeat` seconds

        """
        signal.signal(signal.SIGTERM, lambda signum, stack_frame: sys.exit(1))
        try:
            self.start_heartbeats()
            while self.have_heartbeat:
                if self.w_stats.status == DISABLED:
                    logger.debug('Someone stopped me, sleeping until better'
                                 ' times come (%s)', self.w_stats.sleep)
                    self.sleep()
                    continue
                logger.debug('looping...')
                task = self.wrapped_pop_task()
                if task:
                    self.w_stats.empty_runs = 0
                    self.w_stats.status = RUNNING
                    self.w_stats.total += 1
                    self.wrapped_report_task(task, self.async(task))
                    if not self.w_stats.status == DISABLED:
                        self.w_stats.status = ACTIVE
                else:
                    self.w_stats.empty_runs += 1
                    logger.debug('sleeping...')
                    if self.max_empty_runs != 0:
                        logger.debug('empty runs %s/%s',
                                     self.w_stats.empty_runs,
                                     self.max_empty_runs)
                        if self.w_stats.empty_runs >= self.max_empty_runs:
                            logger.info(
                                'empty runs limit reached, killing myself')
                            self.die()
                    self.sleep()
        except (KeyboardInterrupt, SystemExit):
            logger.info('catched')
            self.die()

    def wrapped_assign_tasks(self, db):
        """Commodity function to call `assign_tasks` and trap exceptions.

        If an exception is raised, assume it happened because of database
        contention and retries `assign_task` after 0.5 seconds
        """
        logger.debug('Assigning tasks...')
        db.commit()  # db.commit() only for Mysql
        x = 0
        while x < 10:
            try:
                self.assign_tasks(db)
                db.commit()
                logger.debug('Tasks assigned...')
                break
            except:
                self.w_stats.errors += 1
                db.rollback()
                logger.error('TICKER: error assigning tasks (%s)', x)
                x += 1
                time.sleep(0.5)

    def wrapped_pop_task(self):
        """Commodity function to call `pop_task` and trap exceptions.

        If an exception is raised, assume it happened because of database
        contention and retries `pop_task` after 0.5 seconds
        """
        db = self.db
        db.commit()  # another nifty db.commit() only for Mysql
        x = 0
        while x < 10:
            try:
                rtn = self.pop_task(db)
                return rtn
                break
            except:
                self.w_stats.errors += 1
                db.rollback()
                logger.error('    error popping tasks')
                x += 1
                time.sleep(0.5)

    def pop_task(self, db):
        """Grab a task ready to be executed from the queue."""
        now = self.now()
        st = self.db.scheduler_task
        if self.is_a_ticker and self.do_assign_tasks:
            # I'm a ticker, and 5 loops passed without reassigning tasks,
            # let's do that and loop again
            self.wrapped_assign_tasks(db)
            return None
        # ready to process something
        grabbed = db(
            (st.assigned_worker_name == self.worker_name) &
            (st.status == ASSIGNED)
        )

        task = grabbed.select(limitby=(0, 1), orderby=st.next_run_time).first()
        if task:
            task.update_record(status=RUNNING, last_run_time=now)
            # noone will touch my task!
            db.commit()
            logger.debug('   work to do %s', task.id)
        else:
            if self.is_a_ticker and self.greedy:
                # there are other tasks ready to be assigned
                logger.info('TICKER: greedy loop')
                self.wrapped_assign_tasks(db)
            else:
                logger.info('nothing to do')
            return None
        times_run = task.times_run + 1
        if task.cronline:
            cron_recur = CronParser(task.cronline, now.replace(second=0))
            next_run_time = cron_recur.get_next()
        elif not task.prevent_drift:
            next_run_time = task.last_run_time + datetime.timedelta(
                seconds=task.period
            )
        else:
            # calc next_run_time based on available slots
            # see #1191
            next_run_time = task.start_time
            secondspassed = (now - next_run_time).total_seconds()
            steps = secondspassed // task.period + 1
            next_run_time += datetime.timedelta(seconds=task.period * steps)

        if times_run < task.repeats or task.repeats == 0:
            # need to run (repeating task)
            run_again = True
        else:
            # no need to run again
            run_again = False
        run_id = 0
        while True and not self.discard_results:
            logger.debug('    new scheduler_run record')
            try:
                run_id = db.scheduler_run.insert(
                    task_id=task.id,
                    status=RUNNING,
                    start_time=now,
                    worker_name=self.worker_name)
                db.commit()
                break
            except:
                time.sleep(0.5)
                db.rollback()
        logger.info('new task %(id)s "%(task_name)s"'
                    ' %(application_name)s.%(function_name)s' % task)
        return Task(
            app=task.application_name,
            function=task.function_name,
            timeout=task.timeout,
            args=task.args,  # in json
            vars=task.vars,  # in json
            task_id=task.id,
            run_id=run_id,
            run_again=run_again,
            next_run_time=next_run_time,
            times_run=times_run,
            stop_time=task.stop_time,
            retry_failed=task.retry_failed,
            times_failed=task.times_failed,
            sync_output=task.sync_output,
            uuid=task.uuid)

    def wrapped_report_task(self, task, task_report):
        """Commodity function to call `report_task` and trap exceptions.

        If an exception is raised, assume it happened because of database
        contention and retries `pop_task` after 0.5 seconds
        """
        db = self.db
        while True:
            try:
                self.report_task(task, task_report)
                db.commit()
                break
            except:
                self.w_stats.errors += 1
                db.rollback()
                logger.error('    error storing result')
                time.sleep(0.5)

    def report_task(self, task, task_report):
        """Take care of storing the result according to preferences.

        Deals with logic for repeating tasks.
        """
        db = self.db
        now = self.now()
        st = db.scheduler_task
        sr = db.scheduler_run
        if not self.discard_results:
            if task_report.result != 'null' or task_report.tb:
                # result is 'null' as a string if task completed
                # if it's stopped it's None as NoneType, so we record
                # the STOPPED "run" anyway
                logger.debug(' recording task report in db (%s)',
                             task_report.status)
                db(sr.id == task.run_id).update(
                    status=task_report.status,
                    stop_time=now,
                    run_result=task_report.result,
                    run_output=task_report.output,
                    traceback=task_report.tb)
            else:
                logger.debug(' deleting task report in db because of no result')
                db(sr.id == task.run_id).delete()
        # if there is a stop_time and the following run would exceed it
        is_expired = (task.stop_time and
                      task.next_run_time > task.stop_time and
                      True or False)
        status = (task.run_again and is_expired and EXPIRED or
                  task.run_again and not is_expired and
                  QUEUED or COMPLETED)
        if task_report.status == COMPLETED:
            d = dict(status=status,
                     next_run_time=task.next_run_time,
                     times_run=task.times_run,
                     times_failed=0
                     )
            db(st.id == task.task_id).update(**d)
            if status == COMPLETED:
                self.update_dependencies(db, task.task_id)
        else:
            st_mapping = {'FAILED': 'FAILED',
                          'TIMEOUT': 'TIMEOUT',
                          'STOPPED': 'FAILED'}[task_report.status]
            status = (task.retry_failed
                      and task.times_failed < task.retry_failed
                      and QUEUED or task.retry_failed == -1
                      and QUEUED or st_mapping)
            db(st.id == task.task_id).update(
                times_failed=st.times_failed + 1,
                next_run_time=task.next_run_time,
                status=status
            )
        logger.info('task completed (%s)', task_report.status)

    def update_dependencies(self, db, task_id):
        """Unblock execution paths for Jobs."""
        db(db.scheduler_task_deps.task_child == task_id).update(can_visit=True)

    def adj_hibernation(self):
        """Used to increase the "sleep" interval for DISABLED workers."""
        if self.w_stats.status == DISABLED:
            wk_st = self.w_stats.sleep
            hibernation = wk_st + HEARTBEAT if wk_st < MAXHIBERNATION else MAXHIBERNATION
            self.w_stats.sleep = hibernation

    def send_heartbeat(self, counter):
        """Coordination among available workers.

        It:
        - sends the heartbeat
        - elects a ticker among available workers (the only process that
            effectively dispatch tasks to workers)
        - deals with worker's statuses
        - does "housecleaning" for dead workers
        - triggers tasks assignment to workers
        """
        if not self.db_thread:
            logger.debug('thread building own DAL object')
            self.db_thread = DAL(
                self.db._uri, folder=self.db._adapter.folder, decode_credentials=True)
            self.define_tables(self.db_thread, migrate=False)
        try:
            db = self.db_thread
            sw, st = db.scheduler_worker, db.scheduler_task
            now = self.now()
            # record heartbeat
            mybackedstatus = db(sw.worker_name == self.worker_name).select().first()
            if not mybackedstatus:
                sw.insert(status=ACTIVE, worker_name=self.worker_name,
                          first_heartbeat=now, last_heartbeat=now,
                          group_names=self.group_names,
                          worker_stats=self.w_stats)
                self.w_stats.status = ACTIVE
                self.w_stats.sleep = self.heartbeat
                mybackedstatus = ACTIVE
            else:
                mybackedstatus = mybackedstatus.status
                if mybackedstatus == DISABLED:
                    # keep sleeping
                    self.w_stats.status = DISABLED
                    logger.debug('........recording heartbeat (%s)',
                                 self.w_stats.status)
                    db(sw.worker_name == self.worker_name).update(
                        last_heartbeat=now,
                        worker_stats=self.w_stats)
                elif mybackedstatus == TERMINATE:
                    self.w_stats.status = TERMINATE
                    logger.debug("Waiting to terminate the current task")
                    self.give_up()
                elif mybackedstatus == KILL:
                    self.w_stats.status = KILL
                    self.die()
                    return
                else:
                    if mybackedstatus == STOP_TASK:
                        logger.info('Asked to kill the current task')
                        self.terminate_process()
                    logger.debug('........recording heartbeat (%s)',
                                 self.w_stats.status)
                    db(sw.worker_name == self.worker_name).update(
                        last_heartbeat=now, status=ACTIVE,
                        worker_stats=self.w_stats)
                    self.w_stats.sleep = self.heartbeat  # re-activating the process
                    if self.w_stats.status != RUNNING:
                        self.w_stats.status = ACTIVE

            self.do_assign_tasks = False
            if counter % 5 == 0 or mybackedstatus == PICK:
                try:
                    # delete dead workers
                    expiration = now - datetime.timedelta(
                        seconds=self.heartbeat * 3)
                    departure = now - datetime.timedelta(
                        seconds=self.heartbeat * 3 * 15)
                    logger.debug(
                        '    freeing workers that have not sent heartbeat')
                    dead_workers = db(
                        ((sw.last_heartbeat < expiration) & (sw.status == ACTIVE)) |
                        ((sw.last_heartbeat < departure) & (sw.status != ACTIVE))
                    )
                    dead_workers_name = dead_workers._select(sw.worker_name)
                    db(
                        (st.assigned_worker_name.belongs(dead_workers_name)) &
                        (st.status == RUNNING)
                    ).update(assigned_worker_name='', status=QUEUED)
                    dead_workers.delete()
                    try:
                        self.is_a_ticker = self.being_a_ticker()
                    except:
                        logger.error('Error coordinating TICKER')
                    if self.w_stats.status == ACTIVE:
                        self.do_assign_tasks = True
                except:
                    logger.error('Error cleaning up')
            db.commit()
        except:
            logger.error('Error retrieving status')
            db.rollback()
        self.adj_hibernation()
        self.sleep()

    def being_a_ticker(self):
        """Elect a TICKER process that assigns tasks to available workers.

        Does its best to elect a worker that is not busy processing other tasks
        to allow a proper distribution of tasks among all active workers ASAP
        """
        db = self.db_thread
        sw = db.scheduler_worker
        my_name = self.worker_name
        all_active = db(
            (sw.worker_name != my_name) & (sw.status == ACTIVE)
        ).select(sw.is_ticker, sw.worker_name)
        ticker = all_active.find(lambda row: row.is_ticker is True).first()
        not_busy = self.w_stats.status == ACTIVE
        if not ticker:
            # if no other tickers are around
            if not_busy:
                # only if I'm not busy
                db(sw.worker_name == my_name).update(is_ticker=True)
                db(sw.worker_name != my_name).update(is_ticker=False)
                logger.info("TICKER: I'm a ticker")
            else:
                # I'm busy
                if len(all_active) >= 1:
                    # so I'll "downgrade" myself to a "poor worker"
                    db(sw.worker_name == my_name).update(is_ticker=False)
                else:
                    not_busy = True
            db.commit()
            return not_busy
        else:
            logger.info(
                "%s is a ticker, I'm a poor worker" % ticker.worker_name)
            return False

    def assign_tasks(self, db):
        """Assign task to workers, that can then pop them from the queue.

        Deals with group_name(s) logic, in order to assign linearly tasks
        to available workers for those groups
        """
        sw, st, sd = db.scheduler_worker, db.scheduler_task, db.scheduler_task_deps
        now = self.now()
        all_workers = db(sw.status == ACTIVE).select()
        # build workers as dict of groups
        wkgroups = {}
        for w in all_workers:
            if w.worker_stats['status'] == 'RUNNING':
                continue
            group_names = w.group_names
            for gname in group_names:
                if gname not in wkgroups:
                    wkgroups[gname] = dict(
                        workers=[{'name': w.worker_name, 'c': 0}])
                else:
                    wkgroups[gname]['workers'].append(
                        {'name': w.worker_name, 'c': 0})
        # set queued tasks that expired between "runs" (i.e., you turned off
        # the scheduler): then it wasn't expired, but now it is
        db(
            (st.status.belongs((QUEUED, ASSIGNED))) &
            (st.stop_time < now)
        ).update(status=EXPIRED)

        # calculate dependencies
        deps_with_no_deps = db(
            (sd.can_visit == False) &
            (~sd.task_child.belongs(
                db(sd.can_visit == False)._select(sd.task_parent)
            )
            )
        )._select(sd.task_child)
        no_deps = db(
            (st.status.belongs((QUEUED, ASSIGNED))) &
            (
                (sd.id == None) | (st.id.belongs(deps_with_no_deps))

            )
        )._select(st.id, distinct=True, left=sd.on(
                 (st.id == sd.task_parent) &
                 (sd.can_visit == False)
        )
        )

        all_available = db(
            (st.status.belongs((QUEUED, ASSIGNED))) &
            (st.next_run_time <= now) &
            (st.enabled == True) &
            (st.id.belongs(no_deps))
        )

        limit = len(all_workers) * (50 / (len(wkgroups) or 1))
        # if there are a moltitude of tasks, let's figure out a maximum of
        # tasks per worker. This can be further tuned with some added
        # intelligence (like esteeming how many tasks will a worker complete
        # before the ticker reassign them around, but the gain is quite small
        # 50 is a sweet spot also for fast tasks, with sane heartbeat values
        # NB: ticker reassign tasks every 5 cycles, so if a worker completes
        # its 50 tasks in less than heartbeat*5 seconds,
        # it won't pick new tasks until heartbeat*5 seconds pass.

        # If a worker is currently elaborating a long task, its tasks needs to
        # be reassigned to other workers
        # this shuffles up things a bit, in order to give a task equal chances
        # to be executed

        # let's freeze it up
        db.commit()
        x = 0
        for group in wkgroups.keys():
            tasks = all_available(st.group_name == group).select(
                limitby=(0, limit), orderby=st.next_run_time)
            # let's break up the queue evenly among workers
            for task in tasks:
                x += 1
                gname = task.group_name
                ws = wkgroups.get(gname)
                if ws:
                    counter = 0
                    myw = 0
                    for i, w in enumerate(ws['workers']):
                        if w['c'] < counter:
                            myw = i
                        counter = w['c']
                    assigned_wn = wkgroups[gname]['workers'][myw]['name']
                    d = dict(
                        status=ASSIGNED,
                        assigned_worker_name=assigned_wn
                    )
                    db(
                        (st.id == task.id) &
                        (st.status.belongs((QUEUED, ASSIGNED)))
                    ).update(**d)
                    wkgroups[gname]['workers'][myw]['c'] += 1
            db.commit()
        # I didn't report tasks but I'm working nonetheless!!!!
        if x > 0:
            self.w_stats.empty_runs = 0
        self.w_stats.queue = x
        self.w_stats.distribution = wkgroups
        self.w_stats.workers = len(all_workers)
        # I'll be greedy only if tasks assigned are equal to the limit
        # (meaning there could be others ready to be assigned)
        self.greedy = x >= limit
        logger.info('TICKER: workers are %s', len(all_workers))
        logger.info('TICKER: tasks are %s', x)

    def sleep(self):
        """Calculate the number of seconds to sleep."""
        time.sleep(self.w_stats.sleep)
        # should only sleep until next available task

    def set_worker_status(self, group_names=None, action=ACTIVE,
                          exclude=None, limit=None, worker_name=None):
        """Internal function to set worker's status."""
        ws = self.db.scheduler_worker
        if not group_names:
            group_names = self.group_names
        elif isinstance(group_names, str):
            group_names = [group_names]
        if worker_name:
            self.db(ws.worker_name == worker_name).update(status=action)
            return
        exclusion = exclude and exclude.append(action) or [action]
        if not limit:
            for group in group_names:
                self.db(
                    (ws.group_names.contains(group)) &
                    (~ws.status.belongs(exclusion))
                ).update(status=action)
        else:
            for group in group_names:
                workers = self.db((ws.group_names.contains(group)) &
                                  (~ws.status.belongs(exclusion))
                                  )._select(ws.id, limitby=(0, limit))
                self.db(ws.id.belongs(workers)).update(status=action)

    def disable(self, group_names=None, limit=None, worker_name=None):
        """Set DISABLED on the workers processing `group_names` tasks.

        A DISABLED worker will be kept alive but it won't be able to process
        any waiting tasks, essentially putting it to sleep.
        By default, all group_names of Scheduler's instantation are selected
        """
        self.set_worker_status(
            group_names=group_names,
            action=DISABLED,
            exclude=[DISABLED, KILL, TERMINATE],
            limit=limit)

    def resume(self, group_names=None, limit=None, worker_name=None):
        """Wakes a worker up (it will be able to process queued tasks)"""
        self.set_worker_status(
            group_names=group_names,
            action=ACTIVE,
            exclude=[KILL, TERMINATE],
            limit=limit)

    def terminate(self, group_names=None, limit=None, worker_name=None):
        """Sets TERMINATE as worker status. The worker will wait for any
        currently running tasks to be executed and then it will exit gracefully
        """
        self.set_worker_status(
            group_names=group_names,
            action=TERMINATE,
            exclude=[KILL],
            limit=limit)

    def kill(self, group_names=None, limit=None, worker_name=None):
        """Sets KILL as worker status. The worker will be killed even if it's
        processing a task."""
        self.set_worker_status(
            group_names=group_names,
            action=KILL,
            limit=limit)

    def queue_task(self, function, pargs=[], pvars={}, **kwargs):
        """
        Queue tasks. This takes care of handling the validation of all
        parameters

        Args:
            function: the function (anything callable with a __name__)
            pargs: "raw" args to be passed to the function. Automatically
                jsonified.
            pvars: "raw" kwargs to be passed to the function. Automatically
                jsonified
            kwargs: all the parameters available (basically, every
                `scheduler_task` column). If args and vars are here, they
                should be jsonified already, and they will override pargs
                and pvars

        Returns:
            a dict just as a normal validate_and_insert(), plus a uuid key
            holding the uuid of the queued task. If validation is not passed
            ( i.e. some parameters are invalid) both id and uuid will be None,
            and you'll get an "error" dict holding the errors found.
        """
        if hasattr(function, '__name__'):
            function = function.__name__
        targs = 'args' in kwargs and kwargs.pop('args') or dumps(pargs)
        tvars = 'vars' in kwargs and kwargs.pop('vars') or dumps(pvars)
        tuuid = 'uuid' in kwargs and kwargs.pop('uuid') or web2py_uuid()
        tname = 'task_name' in kwargs and kwargs.pop('task_name') or function
        immediate = 'immediate' in kwargs and kwargs.pop('immediate') or None
        cronline = kwargs.get('cronline')
        kwargs.update(
            function_name=function,
            task_name=tname,
            args=targs,
            vars=tvars,
            uuid=tuuid,
            )
        if cronline:
            try:
                start_time = kwargs.get('start_time', self.now)
                next_run_time = CronParser(cronline, start_time).get_next()
                kwargs.update(start_time=start_time, next_run_time=next_run_time)
            except:
                pass
        if 'start_time' in kwargs and 'next_run_time' not in kwargs:
            kwargs.update(next_run_time=kwargs['start_time'])
        rtn = self.db.scheduler_task.validate_and_insert(**kwargs)
        if not rtn.errors:
            rtn.uuid = tuuid
            if immediate:
                self.db(
                    (self.db.scheduler_worker.is_ticker == True)
                ).update(status=PICK)
        else:
            rtn.uuid = None
        return rtn

    def task_status(self, ref, output=False):
        """
        Retrieves task status and optionally the result of the task

        Args:
            ref: can be

              - an integer : lookup will be done by scheduler_task.id
              - a string : lookup will be done by scheduler_task.uuid
              - a `Query` : lookup as you wish, e.g. ::

                    db.scheduler_task.task_name == 'test1'

            output(bool): if `True`, fetch also the scheduler_run record

        Returns:
            a single Row object, for the last queued task.
            If output == True, returns also the last scheduler_run record.
            The scheduler_run record is fetched by a left join, so it can
            have all fields == None

        """
        from pydal.objects import Query
        sr, st = self.db.scheduler_run, self.db.scheduler_task
        if isinstance(ref, (int, long)):
            q = st.id == ref
        elif isinstance(ref, str):
            q = st.uuid == ref
        elif isinstance(ref, Query):
            q = ref
        else:
            raise SyntaxError(
                "You can retrieve results only by id, uuid or Query")
        fields = [st.ALL]
        left = False
        orderby = ~st.id
        if output:
            fields = st.ALL, sr.ALL
            left = sr.on(sr.task_id == st.id)
            orderby = ~st.id | ~sr.id
        row = self.db(q).select(
            *fields,
            **dict(orderby=orderby,
                   left=left,
                   limitby=(0, 1))
        ).first()
        if row and output:
            row.result = row.scheduler_run.run_result and \
                loads(row.scheduler_run.run_result,
                      object_hook=_decode_dict) or None
        return row

    def stop_task(self, ref):
        """Shortcut for task termination.

        If the task is RUNNING it will terminate it, meaning that status
        will be set as FAILED.

        If the task is QUEUED, its stop_time will be set as to "now",
            the enabled flag will be set to False, and the status to STOPPED

        Args:
            ref: can be

              - an integer : lookup will be done by scheduler_task.id
              - a string : lookup will be done by scheduler_task.uuid

        Returns:
            - 1 if task was stopped (meaning an update has been done)
            - None if task was not found, or if task was not RUNNING or QUEUED

        Note:
            Experimental
        """
        st, sw = self.db.scheduler_task, self.db.scheduler_worker
        if isinstance(ref, (int, long)):
            q = st.id == ref
        elif isinstance(ref, str):
            q = st.uuid == ref
        else:
            raise SyntaxError(
                "You can retrieve results only by id or uuid")
        task = self.db(q).select(st.id, st.status, st.assigned_worker_name)
        task = task.first()
        rtn = None
        if not task:
            return rtn
        if task.status == 'RUNNING':
            q = sw.worker_name == task.assigned_worker_name
            rtn = self.db(q).update(status=STOP_TASK)
        elif task.status == 'QUEUED':
            rtn = self.db(q).update(
                stop_time=self.now(),
                enabled=False,
                status=STOPPED)
        return rtn

    def get_workers(self, only_ticker=False):
        """ Returns a dict holding `worker_name : {**columns}`
        representing all "registered" workers
        only_ticker returns only the workers running as a TICKER,
        if there are any
        """
        db = self.db
        if only_ticker:
            workers = db(db.scheduler_worker.is_ticker == True).select()
        else:
            workers = db(db.scheduler_worker.id > 0).select()
        all_workers = {}
        for row in workers:
            all_workers[row.worker_name] = Storage(
                status=row.status,
                first_heartbeat=row.first_heartbeat,
                last_heartbeat=row.last_heartbeat,
                group_names=row.group_names,
                is_ticker=row.is_ticker,
                worker_stats=row.worker_stats
            )
        return all_workers


def main():
    """
    allows to run worker without python web2py.py .... by simply::

        python gluon/scheduler.py

    """
    parser = optparse.OptionParser()
    parser.add_option(
        "-w", "--worker_name", dest="worker_name", default=None,
        help="start a worker with name")
    parser.add_option(
        "-b", "--heartbeat", dest="heartbeat", default=10,
        type='int', help="heartbeat time in seconds (default 10)")
    parser.add_option(
        "-L", "--logger_level", dest="logger_level",
        default=30,
        type='int',
        help="set debug output level (0-100, 0 means all, 100 means none;default is 30)")
    parser.add_option("-E", "--empty-runs",
                      dest="max_empty_runs",
                      type='int',
                      default=0,
                      help="max loops with no grabbed tasks permitted (0 for never check)")
    parser.add_option(
        "-g", "--group_names", dest="group_names",
        default='main',
        help="comma separated list of groups to be picked by the worker")
    parser.add_option(
        "-f", "--db_folder", dest="db_folder",
        default='/Users/mdipierro/web2py/applications/scheduler/databases',
        help="location of the dal database folder")
    parser.add_option(
        "-u", "--db_uri", dest="db_uri",
        default='sqlite://storage.sqlite',
        help="database URI string (web2py DAL syntax)")
    parser.add_option(
        "-t", "--tasks", dest="tasks", default=None,
        help="file containing task files, must define" +
        "tasks = {'task_name':(lambda: 'output')} or similar set of tasks")
    parser.add_option(
        "-U", "--utc-time", dest="utc_time", default=False,
        help="work with UTC timestamps"
    )
    (options, args) = parser.parse_args()
    if not options.tasks or not options.db_uri:
        print(USAGE)
    if options.tasks:
        path, filename = os.path.split(options.tasks)
        if filename.endswith('.py'):
            filename = filename[:-3]
        sys.path.append(path)
        print('importing tasks...')
        tasks = __import__(filename, globals(), locals(), [], -1).tasks
        print('tasks found: ' + ', '.join(tasks.keys()))
    else:
        tasks = {}
    group_names = [x.strip() for x in options.group_names.split(',')]

    logging.getLogger().setLevel(options.logger_level)

    print('groups for this worker: ' + ', '.join(group_names))
    print('connecting to database in folder: ' + options.db_folder or './')
    print('using URI: ' + options.db_uri)
    db = DAL(options.db_uri, folder=options.db_folder, decode_credentials=True)
    print('instantiating scheduler...')
    scheduler = Scheduler(db=db,
                          worker_name=options.worker_name,
                          tasks=tasks,
                          migrate=True,
                          group_names=group_names,
                          heartbeat=options.heartbeat,
                          max_empty_runs=options.max_empty_runs,
                          utc_time=options.utc_time)
    signal.signal(signal.SIGTERM, lambda signum, stack_frame: sys.exit(1))
    print('starting main worker loop...')
    scheduler.loop()

if __name__ == '__main__':
    main()
