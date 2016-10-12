from __future__ import unicode_literals
import sys, os, json, uuid, datetime, time, psycopg2, sqlparse
from flask import Flask, request, Response
from threading import Lock, Thread
from collections import defaultdict
from pgcli.pgexecute import PGExecute
from pgspecial import PGSpecial
from pgcli.completion_refresher import CompletionRefresher
from prompt_toolkit.document import Document
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse
special = PGSpecial()
from psycopg2.extensions import (TRANSACTION_STATUS_IDLE,
                                TRANSACTION_STATUS_ACTIVE,
                                TRANSACTION_STATUS_INTRANS,
                                TRANSACTION_STATUS_INERROR,
                                TRANSACTION_STATUS_UNKNOWN)
completers = defaultdict(list)  # Dict mapping urls to pgcompleter objects
completer_lock = Lock()
executors = defaultdict(list)  # Dict mapping buffer ids to pgexecutor objects
executor_lock = Lock()
bufferConnections = defaultdict(str) #Dict mapping bufferids to connectionstrings
queryResults = defaultdict(list)
type_dict = defaultdict(dict)
config = {}
serverList = {}

def main(args=None):
    global serverList
    global config
    try:
        with open('config.json') as json_data_file:
            config = json.load(json_data_file)
            #Todo: load PGCLI using site-dirs from config file.
            serverList = config['connections']
    except Error:
        config = dict()
        serverList = dict()

def to_str(string):
    if sys.version_info < (3,0):
         return unicode(string)
    return str(string)

def new_server(alias, url, requiresauthkey):
    global config
    serverList[alias] = {'url':url, 'requiresauthkey':requiresauthkey}
    config['connections'] = serverList
    with open('config.json', mode='w', encoding='utf-8') as configfile:
        json.dump(config, configfile)

def remove_server(alias):
    global config
    if config['connections'].get(alias):
        del config['connections'][alias]
    if serverList.get(alias):
        del serverList[alias]
    with open('config.json', mode='w', encoding='utf-8') as configfile:
        json.dump(config, configfile)

def connect_server(alias, authkey=None):
    settings = {
        'generate_aliases' : True,
        'casing_file' : os.path.expanduser('~/.config/pgcli/casing'),
        'generate_casing_file' : True,
        'single_connection': True
    }
    server = next((s for (a, s) in serverList.items() if a == alias), None)
    if not server:
        return {'alias': alias, 'success':False, 'errormessage':'Unknown alias.'}
    if executors[alias]:
        return {'alias': alias, 'success':False, 'errormessage':'Already connected to server.'}
    refresher = CompletionRefresher()
    try:
        executor = new_executor(server['url'], authkey)
        with executor.conn.cursor() as cur:
            cur.execute('SELECT oid, oid::regtype::text FROM pg_type')
            type_dict[alias] = dict(row for row in cur.fetchall())
        executors[alias] = executor
        refresher.refresh(executor, special=special, callbacks=(
                            lambda c: swap_completer(c, alias)), settings=settings)
    except psycopg2.Error:
        return {'success':False, 'errormessage':to_str(e)}
    return {'alias': alias, 'success':True, 'errormessage':None}

def refresh_servers():
    for alias, server in serverList.items():
        if alias in executors:
            try:
                if executors.get(alias).conn.closed == 0:
                    server['connected'] = True
                else:
                    server['connected'] = False
                    del executors[alias]
            except Error:
                server['connected'] = False
                del executors[alias]
        else:
            server['connected'] = False

def server_status(alias):
    server = next((s for (a, s) in serverList.items() if a == alias), None)
    if not server:
        return {'alias':alias, 'guid':None, 'success':False, 'errormessage':'Unknown alias.'}
    if executors[alias]:
        if executors[alias].conn.closed == 1:
            server['connected'] = False
            del executors[alias]
            return {'alias':alias, 'guid':None, 'success':False, 'Url':None, 'errormessage':'Not connected.'}
    else:
        return {'alias':alias, 'guid':None, 'success':False, 'Url':None, 'errormessage':None}
    return {'success':True}

def disconnect_server(alias):
    if alias not in executors:
        return {'success':False, 'errormessage':'Unknown alias.'}
    for alias, server in ((a, s) for (a, s) in serverList.items() if a == alias):
        try:
            with executors[alias].conn.cursor() as cur:
                cur.close()
                server['connected'] = False
        except psycopg2.OperationalError:
            server['connected'] = False
            del executors[alias]

def new_executor(url, pwd=None, settings=None):
    uri = urlparse(url)
    database = uri.path[1:]  # ignore the leading fwd slash
    dsn = None  # todo: what is this for again
    return PGExecute(database, uri.username, pwd or uri.password, uri.hostname,
                     uri.port, dsn)

def swap_completer(comp,alias):
    completers[alias] = comp

def format_row(row):
    encoder = json.JSONEncoder()
    columns = []
    for column in row:
        if column is None:
            columns.append(None)
            continue
        try:
            columns.append(encoder.encode(column))
        except TypeError:
            columns.append(to_str(column))
    return tuple(columns)

def get_transaction_status_text(status):
    return {
        TRANSACTION_STATUS_IDLE: 'idle',
        TRANSACTION_STATUS_ACTIVE: 'active',
        TRANSACTION_STATUS_INTRANS: 'intrans',
        TRANSACTION_STATUS_INERROR: 'inerror',
        TRANSACTION_STATUS_UNKNOWN: 'unknown'
    }[status]

def run_sql(alias, sql, uuid):
    for sql in sqlparse.split(sql):
        queryResults[uuid].append({
            'alias': alias,
            'columns': None,
            'rows': None,
            'query': sql,
            'notices': None,
            'statusmessage': None,
            'complete': False,
            'executing': False,
            'timestamp': None,
            'runtime_seconds': None,
            'error':None,
            'transaction_status':None
        })
    executor = executors[alias]
    if not executor:
        return
    with executor_lock:
        with executor.conn.cursor() as cur:
            for n, qr in enumerate(queryResults[uuid]):
                timestamp_ts = time.mktime(datetime.datetime.now().timetuple())
                currentQuery = queryResults[uuid][n]
                currentQuery['timestamp'] = time.strftime('%Y-%m-%d %H:%M:%S')
                currentQuery['executing'] = True
                queryResults[uuid][n] = currentQuery

                #run query
                try:
                    cur.execute(qr['query'])
                except psycopg2.Error:
                    currentQuery['error'] = to_str(e)

                if cur.description:
                    currentQuery['columns'] = [{'name': d.name, 'type_code': d.type_code, 'type': type_dict[alias][d.type_code]} for d in cur.description]
                    currentQuery['rows'] = [format_row(row) for row in cur.fetchall()]

                #update query result
                currentQuery['runtime_seconds'] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
                currentQuery['complete'] = True
                currentQuery['executing'] = False
                currentQuery['statusmessage'] = cur.statusmessage

                notices = []
                while executor.conn.notices:
                    notices.append(executor.conn.notices.pop(0))
                currentQuery['notices'] = notices
                queryResults[uuid][n] = currentQuery

app = Flask(__name__)
@app.route("/query", methods=['POST'])
def query():
    alias = request.form.get('alias', 'Vagrant')
    sql = request.form['query']
    uid = to_str(uuid.uuid1())
    sstatus = server_status(alias)
    if not sstatus['success']:
        return Response(to_str(json.dumps(sstatus)), mimetype='text/json')

    t = Thread(target=run_sql,
                   args=(alias, sql, uid),
                   name='run_sql')
    t.setDaemon(True)
    t.start()
    return Response(to_str(json.dumps({'success':True, 'guid':uid, 'Url':'localhost:5000/result/' + uid, 'errormessage':None})), mimetype='text/json')
@app.route("/result/<uuid>")
def result(uuid):
    result = queryResults[uuid]
    if not result:
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Not connected.'})), mimetype='text/json')
    try:
        for r in result:
            if r['executing'] == True:
                timestamp_ts = time.mktime(datetime.datetime.strptime(r["timestamp"], '%Y-%m-%d %H:%M:%S').timetuple())
                r["runtime_seconds"] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
            r['transaction_status'] = get_transaction_status_text(executors[r['alias']].conn.get_transaction_status())
        return Response(to_str(json.dumps(result)), mimetype='text/json')
    except Error:
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Not connected.'})), mimetype='text/json')

@app.route("/completions", methods=['POST'])
def completions():
    pos = request.form['pos']
    query = request.form['query']
    alias = request.form.get('alias', 'Vagrant')
    if alias in completers:
        comps = completers[alias].get_completions(
                    Document(text=query, cursor_position=int(pos)), None)
        return Response(to_str(json.dumps([{'text': c.text, 'type': c._display_meta} for c in comps])), mimetype='text/json')
    return Response(to_str(json.dumps({'success':False, 'errormessage':'Not connected to server.'})), mimetype='text/json')

@app.route("/listservers")
def list_servers():
    refresh_servers()
    return Response(to_str(json.dumps(serverList)), mimetype='text/json')

@app.route("/listconnections")
def list_connections():
    return Response(to_str(json.dumps(get_connections(), indent=4)), mimetype='text/json')

@app.route("/connect", methods=['POST'])
def connect():
    alias = request.form['alias']
    authkey = request.form['authkey']
    return Response(to_str(json.dumps(connect_server(alias, authkey))), mimetype='text/json')

@app.route("/addserver", methods=['POST'])
def addserver():
    alias = request.form['alias']
    if next((s for (a, s) in serverList.items() if a == alias), None):
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Server alias already exists.'})), mimetype='text/json')
    else:
        url = request.form['url']
        requiresauthkey = request.form['requiresauthkey']
        new_server(alias, url, requiresauthkey)
        return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')

@app.route("/delserver", methods=['POST'])
def delserver():
    try:
        alias = request.form['alias']
        remove_server(alias)
        return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')
    except Exception as e:
        return Response(to_str(json.dumps({'success':False, 'errormessage':str(e)})), mimetype='text/json')

if __name__ == "__main__":
    main()
    app.run()
