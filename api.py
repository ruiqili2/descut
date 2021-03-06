import tornado.auth
import tornado.escape
import tornado.httpserver
import tornado.ioloop
import tornado.options
import tornado.web
import Settings
import pandas as pd
import numpy as np
import os, uuid, io
import json
import sqlite3 as lite
import sys
import datetime as dt
from celery.result import AsyncResult
from celery.task.control import revoke
from expiringdict import ExpiringDict
import binascii
import hashlib
import cx_Oracle
import dtasks
import datetime
import requests
import readfile
import ast


dbConfig0 = Settings.dbConfig()
tokens = ExpiringDict(max_len=300, max_age_seconds=Settings.TOKEN_TTL)


def humantime(s):
    if s < 60:
        return "%d seconds" % s
    else:
        mins = s/60
        secs = s % 60
        if mins < 60:
            return "%d minutes and %d seconds" % (mins, secs)
        else:
            hours = mins/60
            mins  = mins % 60
            if hours < 24:
                return "%d hours and %d minutes" % (hours,mins)
            else:
                days = hours/24
                hours = hours % 24
                return "%d days and %d hours" % (days, hours)

def job_s(entry):
    return entry['job'][entry['job'].index('__')+2:entry['job'].index('_{')]

def dt_t(entry):
    t = dt.datetime.strptime(entry['time'], '%a %b %d %H:%M:%S %Y')
    return t.strftime('%Y-%m-%d %H:%M:%S')

def tup(entry,i, user):
    return (i,user,job_s(entry),entry['status'],dt_t(entry))

class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        return self.get_secure_cookie("usera")



class infoP(object):
    def __init__(self, uu, pp):
        self._uu=uu
        self._pp=pp

def check_permission(password, username, database='desoper'):
    kwargs = {'host': dbConfig0.host, 'port': dbConfig0.port, 'service_name': database}
    dsn = cx_Oracle.makedsn(**kwargs)
    try:
        dbh = cx_Oracle.connect(username, password, dsn=dsn)
        dbh.close()
        return True,""
    except Exception as e:
        error = str(e).strip()
        return False,error


class TokenHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def get(self):
        arguments = { k.lower(): self.get_argument(k) for k in self.request.arguments }
        response = {'status' : 'error'}
        if 'token' in arguments:
            ttl = tokens.ttl(arguments['token'])
            if ttl is None:
                response['message'] = 'Token does not exist or it expired'
                self.set_status(404)
            else:
                response['status'] = 'ok'
                response['message'] = 'Token is valid for %s' % humantime(round(ttl))
                self.set_status(200)
        else:
            response['message'] = 'no token argument found!'
            self.set_status(400)
        self.write(response)
        self.flush()
        self.finish()

    @tornado.web.asynchronous
    def post(self):
        arguments = { k.lower(): self.get_argument(k) for k in self.request.arguments}
        response = {'status' : 'error'}
        if 'username' in arguments:
            if 'password' not in arguments:
                response['message'] = 'Need password'
                self.set_status(400)
            else:
                user = arguments['username']
                passwd = arguments['password']
                check,msg = check_permission(passwd, user)
                if check:
                    response['status'] = 'ok'
                    newfolder = os.path.join(Settings.UPLOADS,user)
                    if not os.path.exists(newfolder):
                        os.mkdir(newfolder)
                else:
                    self.set_status(403)
                    response['message'] = msg
        else:
            response['message'] = 'Need username'
            self.set_status(400)

        if response['status'] == 'ok':
            response['message'] = 'Token created, expiration time: %s' % humantime(Settings.TOKEN_TTL)
            #temp = binascii.hexlify(os.urandom(64))
            temp = hashlib.sha1(os.urandom(64)).hexdigest()
            tokens[temp] = [user,passwd]
            response['token'] = temp
            self.set_status(200)
        self.write(response)
        self.flush()
        self.finish()


class JobHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def post(self):
        arguments = { k.lower(): self.get_argument(k) for k in self.request.arguments }
        response = {'status' : 'error'}
        if 'token' in arguments:
            auths = tokens.get(arguments['token'])
            if auths is None:
                response['message'] = 'Token does not exist or it expired. Please create a new one'
                self.set_status(403)
            else:
                user = auths[0]
                passwd = auths[1]
                response['status'] = 'ok'
                user_folder = os.path.join(Settings.UPLOADS,user) + '/'
        else:
            self.set_status(400)
            response['message'] = 'Need a token to generate a request'

        if response['status'] == 'ok':
            try:
                ra = [float(i) for i in arguments['ra'].replace('[','').replace(']','').split(',')]
                dec = [float(i) for i in arguments['dec'].replace('[','').replace(']','').split(',')]
                stype = "manual"
                if len(ra) != len(dec):
                    self.set_status(400)
                    response['status']='error'
                    response['message'] = 'RA and DEC arrays must have same dimensions'
            except:
                self.set_status(400)
                response['status']='error'
                response['message'] = 'RA and DEC arrays must have same dimensions'

        if response['status'] == 'ok':
            try:
                jtype = arguments['job_type'].lower()
                if jtype not in ('coadd','single'): raise
            except:
                self.set_status(400)
                response['status']='error'
                response['message'] = "Need to specify job_type, either 'coadd' or 'single'"

        if response['status'] == 'ok':
            try:
                fileinfo = self.request.files["csvfile"][0]
                stype = 'csvfile'
            except:
                pass
        # validate bands and convert the correct format
        if response['status'] == 'ok':
            tags = ('y3a1_coadd','y3a1_coadd_deep','y1a1_coadd','y1a1_coadd_d04','y1a1_coadd_d10','y1a1_coadd_dfull','sva1_coadd')
            if 'tag' in arguments:
                try:
                    rtag = arguments['tag'].lower()
                    if rtag not in tags: raise
                except:
                    self.set_status(400)
                    response['status']='error'
                    response['message'] = "Need to specify tag from " +  ",".join(tags)
            else:
                rtag = 'y3a1_coadd'
        if response['status'] == 'ok':
            if 'band' in arguments:
                bands = arguments['band'].replace('[','').replace(']','').replace("'",'').replace(' ','')
                bands_set = set(bands.lower().replace(',',' ').split())
                default_set = set(['g', 'r', 'i', 'z', 'y'])
                if bands_set.issubset(default_set):
                    bands = bands.lower().replace('y', 'Y')
                else:
                    self.set_status(400)
                    response['status']='error'
                    response['message'] = "Optional band must be one of g, r, i, z, Y"
            else:
                bands = 'all'

        if response['status'] == 'ok':
            xs = np.ones(len(ra))
            ys = np.ones(len(ra))
            if 'xsize' in arguments:
                xs_read = [float(i) for i in arguments['xsize'].replace('[','').replace(']','').split(',')]
                if len(xs_read) == 1 : xs=xs*xs_read
                if len(xs) >= len(xs_read): xs[0:len(xs_read)] = xs_read
                else: xs = xs_read[0:len(xs)]
            if 'ysize' in arguments:
                ys_read = [float(i) for i in arguments['ysize'].replace('[','').replace(']','').split(',')]
                if len(ys_read) == 1 : ys=ys*ys_read
                if len(ys) >= len(ys_read): ys[0:len(ys_read)] = ys_read
                else: ys = ys_read[0:len(ys)]
            if 'list_only' in arguments:
                list_only = arguments["list_only"] == 'true'
            else:
                list_only = False
            # include blacklist or not
            if 'no_blacklist' in arguments:
                noBlacklist = arguments["no_blacklist"] == 'true'
            else:
                noBlacklist = False

            if 'email' in arguments:
                send_email = True
                email = arguments['email']
            else:
                send_email = False
            jobid = str(uuid.uuid4())
            if stype=="manual":
                filename = user_folder + jobid + '.csv'
                df = pd.DataFrame(np.array([ra,dec,xs,ys]).T,columns=['RA','DEC','XSIZE','YSIZE'])
                df.to_csv(filename,sep=',',index=False)
                del df
                xs = ""
                ys = ""
            if stype=="csvfile":
                fname = fileinfo['filename']
                extn = os.path.splitext(fname)[1]
                filename = user_folder+jobid+extn
                with open(filename,'w') as F:
                    F.write(fileinfo['body'].decode('ascii'))
                xs = ''
                if 'xsize' in arguments:
                    xs = xs_read[0]
                ys = ''
                if 'ysize' in arguments:
                    ys = ys_read[0]
            folder2 = user_folder+'results/'+jobid+'/'
            os.system('mkdir -p '+folder2)
            infP = infoP(user,passwd)
            now = datetime.datetime.now()
            tiid = user+'__'+jobid+'_{'+now.strftime('%a %b %d %H:%M:%S %Y')+'}'
            comment = arguments['comment']
            #SUBMIT JOB, ADD TO SQLITE
            if jtype == 'coadd':
                tup = tuple([user,jobid,'PENDING',now.strftime('%Y-%m-%d %H:%M:%S'),'Coadd', 0, comment])
                if send_email:
                    run=dtasks.desthumb.apply_async(args=[user_folder + jobid + '.csv', user, passwd, folder2, xs,ys,jobid, list_only, rtag.upper()], task_id=tiid, link=dtasks.send_note.si(user, jobid, email))
                else:
                    run=dtasks.desthumb.apply_async(args=[user_folder + jobid + '.csv', user, passwd, folder2, xs,ys,jobid, list_only, rtag.upper()], task_id=tiid)
            else:
                tup = tuple([user,jobid,'PENDING',now.strftime('%Y-%m-%d %H:%M:%S'),'SE', 0, comment])
                if send_email:
                    run=dtasks.mkcut.apply_async(args=[filename, user, passwd, folder2, xs, ys, bands, jobid, noBlacklist, tiid, list_only], \
                        task_id=tiid, link=dtasks.send_note.si(loc_user, tiid, toemail))
                else:
                    run=dtasks.mkcut.apply_async(args=[filename,user, passwd, folder2, xs, ys, bands, jobid, noBlacklist, tiid, list_only], \
                        task_id=tiid)

            con = lite.connect(Settings.DBFILE)
            with con:
                cur = con.cursor()
                cur.execute("INSERT INTO Jobs VALUES(?, ?, ?, ?, ?, ?, ?)", tup)
            response['message'] = 'Job %s submitted.' % (jobid)
            response['job'] = jobid
            try:
                readfile.notify(user,jobid)
            except:
                pass
            self.set_status(200)

        self.write(response)
        self.flush()
        self.finish()


    @tornado.web.asynchronous
    def get(self):
        arguments = { k.lower(): self.get_argument(k) for k in self.request.arguments }
        response = {'status' : 'error'}
        if 'token' in arguments:
            auths = tokens.get(arguments['token'])
            if auths is None:
                response['message'] = 'Token does not exist or it expired. Please create a new one'
                self.set_status(403)
            else:
                user = auths[0]
                passwd = auths[1]
                response['status'] = 'ok'
                user_folder = os.path.join(Settings.UPLOADS,user) + '/'
        else:
            self.set_status(400)
            response['message'] = 'Need a token to generate a request'

        if response['status'] == 'ok':
            if 'list_jobs' in arguments:
                con = lite.connect(Settings.DBFILE)
                with con:
                    cur = con.cursor()
                    cc = cur.execute("SELECT job, datetime(time), status, type  from Jobs where user = '%s' order by datetime(time) DESC  " % user).fetchall()
                response['message'] = 'List of jobs returned'
                response['list_jobs'] = [j[0] for j in cc]
                response['creation_time'] = [j[1] for j in cc]
                response['job_status'] = [j[2] for j in cc]
                response['job_type'] = [j[3] for j in cc]
                response['job_public'] = [j[4] for j in cc]
                response['job_comment'] = [j[5] for j in cc]
                self.set_status(200)
                self.write(response)
                self.flush()
                self.finish()




        if response['status'] == 'ok':
            if 'jobid' in arguments:
                jobid = arguments['jobid']
            else:
                response['status'] = 'error'
                self.set_status(400)
                response['message'] = 'No jobid argument in request'

        if response['status'] == 'ok':
            con = lite.connect(Settings.DBFILE)
            with con:
                cur = con.cursor()
                cc = cur.execute("SELECT status from Jobs where user = '%s' and job = '%s'" % (user, jobid)).fetchall()
            try:
                status = cc[0][0]
                response['message'] = 'Job %s is %s' % (jobid, status)
                response['job_status'] = status
                if status  == 'SUCCESS':
                    list_file = user_folder + 'results/'+jobid+'/list_all.txt'
                    with open(list_file) as f:
                        links = f.read().splitlines()
                    response['links'] = links
                self.set_status(200)
            except:
                response['status'] = 'error'
                self.set_status(400)
                response['message'] = 'Job Id does not exists'

        self.write(response)
        self.flush()
        self.finish()


    @tornado.web.asynchronous
    def delete(self):
        arguments = { k.lower(): self.get_argument(k) for k in self.request.arguments }
        response = {'status' : 'error'}
        if 'token' in arguments:
            auths = tokens.get(arguments['token'])
            if auths is None:
                response['message'] = 'Token does not exist or it expired. Please create a new one'
                self.set_status(403)
            else:
                user = auths[0]
                passwd = auths[1]
                response['status'] = 'ok'
                user_folder = os.path.join(Settings.UPLOADS,user) + '/'
        else:
            response['status'] = 'error'
            self.set_status(400)
            response['message'] = 'Need a token to generate a request'

        if response['status'] == 'ok':
            if 'jobid' in arguments:
                jobid = arguments['jobid']
            else:
                self.set_status(400)
                response['message'] = 'No jobid argument in request'

        if response['status'] == 'ok':
            con = lite.connect(Settings.DBFILE)
            with con:
                cur = con.cursor()
                cc = cur.execute("SELECT status from Jobs where user = '%s' and job = '%s'" % (user, jobid)).fetchall()
            try:
                status = cc[0][0]
                with con:
                    cur = con.cursor()
                    cc = cur.execute("DELETE from Jobs where user = '%s' and job = '%s'" % (user, jobid))
                response['message'] = 'Job %s was deleted' % (jobid)
                folder = os.path.join(user_folder,'results/' + jobid)
                os.system('rm -rf ' + folder)
                os.system('rm -f ' + os.path.join(user_folder,jobid+'.csv'))
                self.set_status(200)
                try:
                    readfile.notify(user,jobid)
                except:
                    pass
            except:
                response['status'] = 'error'
                self.set_status(400)
                response['message'] = 'Job Id does not exists'

        self.write(response)
        self.flush()
        self.finish()


class MongoHandler(tornado.web.RequestHandler):

    @tornado.web.asynchronous
    def post(self):

        arguments = { k.lower(): self.get_argument(k) for k in self.request.arguments }
        response = {'status' : 'error'}
        qParam = {}

        if 'token' in arguments:
            auths = tokens.get(arguments['token'])
            if auths is None:
                response['message'] = 'Token does not exist or it expired. Please create a new one'
                self.set_status(403)
            else:
                user = auths[0]
                passwd = auths[1]
                response['status'] = 'ok'
                user_folder = os.path.join(Settings.UPLOADS,user) + '/'
        else:
            self.set_status(400)
            response['message'] = 'Need a token to generate a request'

        if response['status'] == 'ok':
            if 'band' in arguments:
                bands = arguments['band'].replace('[','').replace(']','').replace("'",'').replace(' ','')
                bands_set = set(bands.lower().split(','))
                default_set = set(['g', 'r', 'i', 'z', 'y'])
                if bands_set.issubset(default_set):
                    bands = bands.lower().replace('y', 'Y')
                    qParam['bands'] = bands
                else:
                    self.set_status(400)
                    response['status']='error'

        if response['status'] == 'ok':
            if 'ra' in arguments and 'dec' in arguments:
                try:
                    ra = [float(i) for i in arguments['ra'].replace('[','').replace(']','').split(',')]
                    dec = [float(i) for i in arguments['dec'].replace('[','').replace(']','').split(',')]
                    df_pos = pd.DataFrame(np.array([ra,dec]).T,columns=['RA','DEC'])
                    qParam['df_pos'] = df_pos
                    # stype = "manual"
                    if len(ra) != len(dec):
                        self.set_status(400)
                        response['status']='error'
                        response['message'] = 'RA and DEC arrays must have same dimensions'
                except:
                    self.set_status(400)
                    response['status']='error'
                    response['message'] = 'RA and DEC arrays must have same dimensions'
            elif 'expnum' in arguments:
                try:
                    expnum = [int(i) for i in arguments['expnum'].replace('[','').replace(']','').split(',')]
                    qParam['expnum'] = expnum
                    arguments.pop('expnum')
                except:
                    self.set_status(400)
                    response['status']='error'
                    response['message'] = "Invalid expnum!"
            elif 'nite' in arguments:
                try:
                    night = [int(i) for i in arguments['nite'].replace('[','').replace(']','').split(',')]
                    qParam['night'] = night
                    arguments.pop('nite')
                except:
                    self.set_status(400)
                    response['status']='error'
                    response['message'] = "Invalid nite!"
            else:
                response['status'] = 'error'
                response['message'] = 'Missing input: at least one of expnum, nite or (ra, dec) has to be specified for the query!'

        #extra options
        if response['status'] == 'ok':

            if 'no_blacklist' in arguments:
                noBlacklist = arguments["no_blacklist"] == 'true'
            else:
                noBlacklist = False

            if 'ccdnum' in arguments:
                try:
                    ccdnum = [int(i) for i in arguments['ccdnum'].replace('[','').replace(']','').split(',')]
                    qParam['ccdnum'] = ccdnum
                except:
                    self.set_status(400)
                    response['status']='error'
                    response['message'] = "Invalid ccdnum!"

            if 'nite' in arguments:
                try:
                    night = [int(i) for i in arguments['nite'].replace('[','').replace(']','').split(',')]
                    qParam['night'] = night
                except:
                    self.set_status(400)
                    response['status']='error'
                    response['message'] = "Invalid nite!"

            if 'expnum' in arguments:
                try:
                    expnum = [int(i) for i in arguments['expnum'].replace('[','').replace(']','').split(',')]
                    qParam['expnum'] = expnum
                except:
                    self.set_status(400)
                    response['status']='error'
                    response['message'] = "Invalid expnum!"



        if response['status'] == 'ok':

            #init jobid
            qTaskId = str(uuid.uuid4())
            # put ra, dec into a datafram
            now = datetime.datetime.now()
            qTiid = user+'_mongo_'+qTaskId+'_{'+now.strftime('%a %b %d %H:%M:%S %Y')+'}'
            # print (qParam)
            try:
                dtasks.getList.apply_async(args=[noBlacklist, qParam], task_id=qTiid)
            except Exception as e:
                response['status']='error'
                response['message']=str(e)
            else:
                res = AsyncResult(qTiid)
                result = res.wait(10)
                df_rt = pd.read_json(result)
                df_rt = df_rt[['RA_CENT', 'DEC_CENT', 'FILENAME', 'BAND','EXPNUM', 'NITE','PFW_ATTEMPT_ID', 'CCDNUM', 'FULL_PATH']]
                response['list']= df_rt.to_dict(orient='records')
                self.set_status(200)

        self.write(response)
        self.flush()
        self.finish()


class ShareHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self):
        # loc_user = self.get_secure_cookie("usera").decode('ascii').replace('\"','')
        response = { k: self.get_argument(k) for k in self.request.arguments }
        con = lite.connect(Settings.DBFILE)
        with con:
            cur = con.cursor()
            cc = cur.execute("SELECT * from Jobs where public = %d order by datetime(time) DESC " % 1).fetchall()
        cc = list(cc)
        jjob=[]
        jstatus=[]
        jtime=[]
        jelapsed=[]
        jtype=[]
        jpublic=[]
        jcomment=[]

        for i in range(len(cc)):
            dd = dt.datetime.strptime(cc[i][3],'%Y-%m-%d %H:%M:%S')
            ctime = dd.strftime('%a %b %d %H:%M:%S %Y')
            jjob.append(cc[i][0]+'__'+cc[i][1]+'_{'+ctime+'}')
            jstatus.append(cc[i][2])
            jtime.append(ctime)
            jtype.append(cc[i][4])
            jelapsed.append(humantime((dt.datetime.now()-dd).total_seconds())+" ago")
            jpublic.append(cc[i][5])
            jcomment.append(cc[i][6])

        out_dict=[dict(job=jjob[i],status=jstatus[i], time=jtime[i], elapsed=jelapsed[i], jtypes=jtype[i], jpublic=jpublic[i], jcomment=jcomment[i]) for i in range(len(jjob))]
        temp = json.dumps(out_dict, indent=4)
            #with open('static/jobs2.json',"w") as outfile:
        self.write(temp)
            #self.set_status(200)
            #self.flush()
            #self.finish()


class ApiHandler(BaseHandler):
    @tornado.web.authenticated
    def delete(self):
        loc_user = self.get_secure_cookie("usera").decode('ascii').replace('\"','')
        user_folder = os.path.join(Settings.UPLOADS,loc_user)
        response = { k: self.get_argument(k) for k in self.request.arguments }
        Nd=len(response)
        con = lite.connect(Settings.DBFILE)
        with con:
            cur = con.cursor()
            for j in range(Nd):
                jid=response[str(j)]
                q = "DELETE from Jobs where job = '%s' and user = '%s'" % (jid, loc_user)
                cc = cur.execute(q)
                folder = os.path.join(user_folder,'results/' + jid)
                try:
                    os.system('rm -rf ' + folder)
                    os.system('rm -f ' + os.path.join(user_folder,jid+'.csv'))
                except:
                    pass
        self.set_status(200)
        self.flush()
        self.finish()

    @tornado.web.authenticated
    def get(self):
        loc_user = self.get_secure_cookie("usera").decode('ascii').replace('\"','')
        response = { k: self.get_argument(k) for k in self.request.arguments }
        con = lite.connect(Settings.DBFILE)
        with con:
            cur = con.cursor()
            cc = cur.execute("SELECT * from Jobs where user = '%s' order by datetime(time) DESC " % loc_user).fetchall()
        cc = list(cc)
        jjob=[]
        jstatus=[]
        jtime=[]
        jelapsed=[]
        jtype=[]
        jpublic=[]
        jcomment=[]

        for i in range(len(cc)):
            dd = dt.datetime.strptime(cc[i][3],'%Y-%m-%d %H:%M:%S')
            ctime = dd.strftime('%a %b %d %H:%M:%S %Y')
            jjob.append(cc[i][0]+'__'+cc[i][1]+'_{'+ctime+'}')
            jstatus.append(cc[i][2])
            jtime.append(ctime)
            jtype.append(cc[i][4])
            jelapsed.append(humantime((dt.datetime.now()-dd).total_seconds())+" ago")
            jpublic.append(cc[i][5])
            jcomment.append(cc[i][6])

        out_dict=[dict(job=jjob[i],status=jstatus[i], time=jtime[i], elapsed=jelapsed[i], jtypes=jtype[i], jpublic=jpublic[i], jcomment=jcomment[i]) for i in range(len(jjob))]
        temp = json.dumps(out_dict, indent=4)
            #with open('static/jobs2.json',"w") as outfile:
        self.write(temp)
            #self.set_status(200)
            #self.flush()
            #self.finish()


class LogHandler(BaseHandler):

    @tornado.web.authenticated
    def get(self):
        loc_user = self.get_secure_cookie("usera").decode('ascii').replace('\"','')
        jobidFull = self.get_argument("jobid")
        jobid = jobidFull[jobidFull.find('__')+2:jobidFull.find('{')-1]
        log_path=os.path.join(Settings.UPLOADS,loc_user, 'results', jobid, 'log.log')
        res = AsyncResult(jobidFull)

        log = ''

        if res.ready():
            with open(log_path, 'r') as logFile:
                for line in logFile:
                    log+=line+'<br>'
            temp = json.dumps(log)
        else:
            temp = json.dumps('Running')
        # if res is not None:
        #     try:
        #         temp = json.dumps(res.result.replace('\n','</br>'))
        #     except:
        #         temp = 'Running'
        # else:
        #     temp = json.dumps('Running')

        self.write(temp)

class CancelJobHandler(BaseHandler):

    @tornado.web.authenticated
    def delete(self):
        loc_user = self.get_secure_cookie("usera").decode('ascii').replace('\"','')
        jobid = self.get_argument("jobid")
        jobid2=jobid[jobid.find('__')+2:jobid.find('{')-1]
        revoke(jobid, terminate=True)
        con = lite.connect(Settings.DBFILE)
        with con:
            cur = con.cursor()
            q = "UPDATE Jobs SET status='REVOKED' where job = '%s'" % jobid2
            cc = cur.execute(q)
        self.set_status(200)
        self.flush()
        self.finish()

class ShareJobHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self):
        loc_user = self.get_secure_cookie("usera").decode('ascii').replace('\"', '')
        user_folder = os.path.join(Settings.UPLOADS, loc_user)
        response = {k: self.get_argument(k) for k in self.request.arguments}
        Nd = len(response)
        con = lite.connect(Settings.DBFILE)
        with con:
            cur = con.cursor()
            for j in range(Nd):
                jid = response[str(j)]
                q = "UPDATE Jobs SET public=%d where job = '%s'" % (1, jid)
                cc = cur.execute(q)
                folder = os.path.join(user_folder, 'results/' + jid)

        self.set_status(200)
        self.flush()
        self.finish()

    @tornado.web.authenticated
    def delete(self):
        loc_user = self.get_secure_cookie("usera").decode('ascii').replace('\"','')
        user_folder = os.path.join(Settings.UPLOADS,loc_user)
        response = { k: self.get_argument(k) for k in self.request.arguments }
        Nd=len(response)
        con = lite.connect(Settings.DBFILE)
        with con:
            cur = con.cursor()
            for j in range(Nd):
                jid=response[str(j)]
                q = "UPDATE Jobs SET public=%d where job = '%s'" % (0, jid)
                cc = cur.execute(q)
                folder = os.path.join(user_folder,'results/' + jid)

        self.set_status(200)
        self.flush()
        self.finish()

class AddCommentHandler(BaseHandler):
    @tornado.web.authenticated
    @tornado.web.asynchronous
    def post(self):
        loc_user = self.get_secure_cookie("usera").decode('ascii').replace('\"', '')
        user_folder = os.path.join(Settings.UPLOADS, loc_user)
        comment = self.get_argument("comment")
        print("*******")
        print(comment)
        jobid = self.get_argument("jobid")
        print(jobid)
        print("*******")

        # response = {k: self.get_argument(k) for k in  self.request.arguments}
        # Nd = len(response)
        con = lite.connect(Settings.DBFILE)
        with con:
            cur = con.cursor()
            # for j in range(Nd):
                # jid = response[str(j)]
            q = "UPDATE Jobs SET comment='%s' where job = '%s'" % (comment, jobid)
            check = "select * from Jobs where job = '%s'" % jobid
            cc = cur.execute(q)
            cc = cur.execute(check).fetchall()
            print(cc)
            folder = os.path.join(user_folder, 'results/' + jobid)

        self.set_status(200)
        self.flush()
        self.finish()
