#!/usr/bin/python
# -*- coding: utf-8 -*-

__description__ = 'Malcom - Malware communications analyzer'
__author__ = '@tomchop_'
__version__ = '1.0 alpha'
__license__ = "GPL"


# custom
from toolbox import *
from analytics import *
from feeds.feed import FeedEngine
from datatypes.element import Hostname
from networking import netsniffer

#db 
from pymongo import MongoClient

#json / bson
from bson.objectid import ObjectId
from bson.json_util import dumps, loads

#system
import os, datetime, time, sys, signal, argparse, re
import netifaces as ni

#flask stuff
from werkzeug import secure_filename
from flask import Flask, request, render_template, redirect, url_for, g, make_response, abort, flash
from functools import wraps


#websockets
from geventwebsocket.handler import WebSocketHandler
from gevent.pywsgi import WSGIServer


# for file upload
ALLOWED_EXTENSIONS = set(['txt', 'csv'])

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.debug = True

# This enables the server to be ran behind a reverse-proxy
# Make sure you have an nginx configuraiton similar to this

# location = /malcom { rewrite ^ /malcom/; }
# location /malcom { try_files $uri @malcom; }

# # proxy
# location @malcom {
# 	proxy_pass http://127.0.0.1:8080;
# 	proxy_http_version 1.1;
# 	proxy_set_header SCRIPT_NAME /malcom;
# 	proxy_set_header Host $host;    
# 	proxy_set_header X-Scheme $scheme;
# 	proxy_set_header Upgrade $http_upgrade;
# 	proxy_set_header Connection "upgrade";
# }

def malcom_app(environ, start_response):  
	
	if environ.get('HTTP_SCRIPT_NAME'):
		# update path info 
		environ['PATH_INFO'] = environ['PATH_INFO'].replace(environ['HTTP_SCRIPT_NAME'], "")
		# declare SCRIPT_NAME
		environ['SCRIPT_NAME'] = environ['HTTP_SCRIPT_NAME']
	
	if environ.get('HTTP_X_SCHEME'):	
		# forward the scheme
		environ['wsgi.url_scheme'] = environ.get('HTTP_X_SCHEME')

	return app(environ, start_response)


app.config['DEBUG'] = True
app.config['VERSION'] = "1.0 alpha"
app.config['UPLOAD_FOLDER'] = ""
app.config['LISTEN_INTERFACE'] = "0.0.0.0"
app.config['LISTEN_PORT'] = 8080
app.config['MAX_THREADS'] = 4
app.config['PUBLIC'] = False
app.config['NO_FEED'] = False


app.config['IFACES'] = {}
for i in [i for i in ni.interfaces() if i.find('eth') != -1]:
	app.config['IFACES'][i] = ni.ifaddresses(i).get(2,[{'addr':'Not defined'}])[0]['addr']

# global avariables, used throughout malcom
sniffer_sessions = {}
analytics_engine = Analytics()
feed_engine = FeedEngine(analytics_engine)

@app.errorhandler(404)
def page_not_found(error):
	return 'This page does not exist', 404

@app.after_request
def after_request(response):
	origin = request.headers.get('Origin', '')
	# debug_output(origin, False)
	response.headers['Access-Control-Allow-Origin'] = origin
	response.headers['Access-Control-Allow-Credentials'] = 'true'
	return response

@app.before_request
def before_request():
	# make configuration and analytics engine available to views
	g.config = app.config
	g.a = analytics_engine


# decorator for URLs that should not be public
def private_url(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if app.config['PUBLIC']:
            abort(404)
        return f(*args, **kwargs)
    return decorated_function


@app.route('/')
def index():
	return redirect(url_for('dataset'))

# feeds ========================================================

@app.route('/feeds')
@private_url
def feeds():
	alpha = sorted(feed_engine.feeds, key=lambda name: name)
	return render_template('feeds.html', feed_names=alpha, feeds=feed_engine.feeds)

@app.route('/feeds/run/<feed_name>')
@private_url
def run_feed(feed_name):
	feed_engine.run_feed(feed_name)
	return redirect(url_for('feeds'))


# graph operations =============================================

@app.route('/nodes/<field>/<path:value>')
def nodes(field, value):
	return render_template('dynamic_nodes.html', field=field, value=value)

@app.route('/graph/<field>/<path:value>')
def graph(field, value):
	a = g.a
	#query = { field: re.compile(re.escape(value), re.IGNORECASE) }
	# faster query
	query = { field: value }
	base_elts = [e for e in a.data.elements.find( query )]

	total_nodes = []
	total_edges = []
	nodes = []
	edges = []
	for elt in base_elts:
		nodes, edges = a.data.get_neighbors(elt)
		total_nodes.extend(nodes)
		total_edges.extend(edges)

	data = { 'query': base_elts, 'edges': total_edges, 'nodes': total_nodes }
	ids = [node['_id'] for node in nodes]
	
	debug_output("query: %s, edges found: %s, nodes found: %s" % (len(base_elts), len(edges), len(nodes)))
	return (dumps(data))

@app.route('/neighbors', methods=['POST'])
def neighbors():
	a = g.a
	allnodes = []
	alledges = []
	msg = ""
	if len(request.form.getlist('ids')) == 0:
		return dumps({})

	for id in request.form.getlist('ids'):
		elt = a.data.elements.find_one({'_id': ObjectId(id) })
		nodes, edges = a.data.get_neighbors(elt)
		if len(nodes) > 2000 or len(edges) > 2000:
			msg = "TOO_MANY_ELEMENTS" # at least, we notify the user that we're doing something dirty
		allnodes += [n for n in nodes[:2000] if n not in allnodes] # this is a really expensive operation
		alledges += [e for e in edges[:2000] if e not in alledges] # dirty solution, limit to 1000 results
		
	data = { 'query': elt, 'nodes':allnodes, 'edges': alledges, 'msg': msg }

	return (dumps(data))

@app.route('/evil', methods=['POST'])
def evil():
	a = g.a
	allnodes = []
	alledges = []
	msg = ""
	for id in request.form.getlist('ids'):
		elt = a.data.elements.find_one({'_id': ObjectId(id) })
		nodes, edges = a.find_evil(elt)
		allnodes += [n for n in nodes if n not in allnodes]
		alledges += [e for e in edges if e not in alledges]
		
	data = { 'query': elt, 'nodes':allnodes, 'edges': alledges, 'msg': msg }

	return (dumps(data))


# dataset operations ======================================================

def allowed_file(filename):
	return '.' in filename and \
		   filename.rsplit('.', 1)[1] in ALLOWED_EXTENSIONS

@app.route('/dataset/')
def dataset():
	return render_template("dataset.html")


@app.route('/dataset/list/') # ajax method for sarching dataset and populating dataset table
def list():
	a = g.a
	query = {}
	try:
		page = int(request.args['page'])
	except Exception, e:
		page = 0

	fuzzy = False if request.args['fuzzy']=='false' else True

	for key in request.args:
		if key not in  ['page', 'fuzzy']:
			if request.args[key].find(',') != -1: # split request arguments
				if fuzzy:
					query['$and'] = [{ key: re.compile(split, re.IGNORECASE)} for split in request.args[key].split(',')]
				else:
					query['$and'] = [{ key: split} for split in request.args[key].split(',')]
			else:
				if fuzzy:
					query[key] = re.compile(request.args[key], re.IGNORECASE) # {"$regex": request.args[key]}
				else:
					query[key] = request.args[key]

	per_page = 50

	chrono_query = datetime.datetime.now()
	elts = [e for e in a.data.find(query).sort('date_created', -1)[page*per_page:page*per_page+per_page]]
	chrono_query = datetime.datetime.now() - chrono_query
	debug_output("Query completed in %s" % chrono_query)
	
	
	for elt in elts:
		elt['link_value'] = url_for('nodes', field='value', value=elt['value'])
		elt['link_type'] = url_for('nodes', field='type', value=elt['type'])

	data = {}
	if len(elts) > 0:
		data['fields'] = elts[0].display_fields
		data['elements'] = elts
	else:
		data['fields'] = [('value', 'Value'), ('type', 'Type'), ('tags', 'Tags')]
		data['elements'] = []
	
	data['page'] = page
	data['per_page'] = per_page

	chrono_count = datetime.datetime.now()
	data['total_results'] = a.data.find(query).count()
	chrono_count = datetime.datetime.now() - chrono_count
	debug_output("Count completed in %s" % chrono_count)
	data['chrono_query'] = str(chrono_query)
	data['chrono_count'] = str(chrono_count)
	return dumps(data)

@app.route('/dataset/list/csv')
def dataset_csv():
	a = g.a
	filename = []
	query = {}
	fuzzy = False if request.args['fuzzy'] == 'false' else True

	for key in request.args:
		if key != '' and key not in ['fuzzy']:
			if fuzzy:
				# slow
				query[key] = re.compile(re.escape(request.args[key]), re.IGNORECASE)
			else:
				# skip regex to make it faster
				query[key] = request.args[key]
			filename.append("%s_%s" % (key, request.args[key]))
		else:
			filename.append('all')

	filename = "-".join(filename)
	results = a.data.find(query).sort('date_created', -1)
	
	if results.count() == 0:
		flash("You're about to download an empty .csv",'warning')
		return redirect(url_for('dataset'))
	else:
		response = make_response()
		response.headers['Cache-Control'] = 'no-cache'
		response.headers['Content-Type'] = 'text/csv'
		response.headers['Content-Disposition'] = 'attachment; filename='+filename+'-extract.csv'
		fields = results[0].display_fields
		data = ";".join([f[1] for f in fields ]) + "\n"
		for e in results:
			data += ";".join([list_to_str(e.get(f[0],"-")) for f in fields]) + "\n"

		response.data = data
		response.headers['Content-Length'] = len(response.data)

		return response


@app.route('/dataset/add', methods=['POST'])
@private_url
def add_data():
	
	if request.method == "POST":
		file = request.files.get('element-list')
		if file:  #we're dealing with a list of elements
			if allowed_file(file.filename):
				elements = file.read()
				elements = elements.split("\n")
			else:
				return 'filename not allowed'
		else:
			elements = [request.form['element']]

		tags = request.form.get('tags', None)
		
		if len(elements) == 0 or not tags:
			flash("You must specify an element and tags", 'warning')
			return redirect(url_for('dataset'))

		a = g.a
		tags = tags.strip().split(";")
		a.add_text(elements, tags)

		if request.form.get('analyse', None):
			a.process()

		return redirect(url_for('dataset'))

	else:
		return "Not allowed"

@app.route('/dataset/remove/<id>')
def delete(id):
	a = g.a 
	result = a.data.remove(id)
	return dumps(result)

@app.route('/dataset/clear/')
@private_url
def clear():
	g.a.data.clear_db()
	return redirect(url_for('dataset'))

@app.route('/analytics')
def analytics():
	g.a.process()
	return "Analytics: Done."

# Sniffer ============================================

@app.route('/sniffer/',  methods=['GET', 'POST'])
def sniffer():
	if request.method == 'POST':
		filter = request.form['filter']
		
		session_name = request.form['session_name']
		if session_name == "":
			flash("Please specify a session name", 'warning')
			return redirect(url_for('sniffer'))

		debug_output("Creating session %s" % session_name)
		sniffer_sessions[session_name] = netsniffer.Sniffer(Analytics(), session_name, str(request.remote_addr), filter, g.config['IFACES'])
		
		# if we're dealing with an uploaded PCAP file
		file = request.files.get('pcap-file')

		if file:
			pcap = file.read()
			loaded = sniffer_sessions[session_name].load_pcap(pcap)
			if loaded != True:
				flash("Could not read .pcap file: %s" % loaded, 'error')
				return redirect(url_for('sniffer'))

		# start sniffing right away
		if request.form.get('startnow', None):
			sniffer_sessions[session_name].start(str(request.remote_addr))
		
		return redirect(url_for('sniffer_session', session_name=session_name))


	return render_template('sniffer_new.html')

@app.route('/sniffer/sessionlist/')
def sniffer_sessionlist():
	session_list = []
	for s in sniffer_sessions:
		session_list.append({
								'name': s, 
								'packets': len(sniffer_sessions[s].pkts),
								'nodes': len(sniffer_sessions[s].nodes),
								'edges': len(sniffer_sessions[s].edges),
							})
	return dumps({'session_list': session_list})


@app.route('/sniffer/<session_name>/')
def sniffer_session(session_name):
	# check if session exists
	if session_name not in sniffer_sessions:
		flash("Sniffing session '%s' does not exist" % session_name, 'warning')
		return redirect(url_for('sniffer'))
	
	return render_template('sniffer.html', session=sniffer_sessions[session_name], session_name=session_name)
	

@app.route('/sniffer/<session_name>/pcap')
def pcap(session_name):
	if session_name not in sniffer_sessions:
		abort(404)
	response = make_response()
	response.headers['Cache-Control'] = 'no-cache'
	response.headers['Content-Type'] = 'application/vnd.tcpdump.pcap'
	response.headers['Content-Disposition'] = 'attachment; filename='+session_name+'capture.pcap'
	response.data = sniffer_sessions[session_name].get_pcap()
	response.headers['Content-Length'] = len(response.data)

	return response



# APIs =========================================


@app.route('/api/analytics')
def analytics_api():
	debug_output("Call to analytics API")

	if request.environ.get('wsgi.websocket'):
		debug_output("Got websocket")

		ws = request.environ['wsgi.websocket']
		g.a.websocket = ws

		while True:
			try:
				message = loads(ws.receive())
				debug_output("Received: %s" % message)
			except Exception, e:
				return ""

			cmd = message['cmd']

			if cmd == 'analyticsstatus':
				g.a.notify_progress()
				# if g.a.active:
				# 	send_msg(ws, {'status': 1}, type=cmd)
				# else:
				# 	send_msg(ws, {'status': 0}, type=cmd)

			


@app.route('/api/sniffer')
def sniffer_api():
	debug_output("call to sniffer API")

	if request.environ.get('wsgi.websocket'):

		ws = request.environ['wsgi.websocket']

		while True:
			try:
				message = loads(ws.receive())
			except Exception, e:
				debug_output("Could not decode JSON message: %s" %e)
				return ""
			
			debug_output("Received: %s" % message)



			cmd = message['cmd']
			session_name = message['session_name']

			if session_name in sniffer_sessions:
				session = sniffer_sessions[session_name]
			else:
				send_msg(ws, "Session %s not foud" % session_name, type=cmd)
				continue

			session.ws = ws


			# websocket commands

			if cmd == 'sessionlist':
				session_list = [s for s in sniffer_sessions]
				send_msg(ws, {'session_list': session_list}, type=cmd)
				continue

			if cmd == 'sniffstart':
				if g.config['PUBLIC']:
					continue
				session.start(str(request.remote_addr))
				send_msg(ws, "OK", type=cmd)
				continue

			if cmd == 'sniffstop':
				if g.config['PUBLIC']:
					continue
				if session.status():
					session.stop()
					send_msg(ws, 'OK', type=cmd)
				else:
					send_msg(ws, 'Error: sniffer not running', type=cmd)
				continue

			if cmd == 'sniffstatus':
				if session.status():
					status = 'active'
					debug_output("Session %s is active" % session.name)
					send_msg(ws, {'status': 'active', 'session_name': session.name}, type=cmd)
				else:
					status = 'inactive'
					debug_output("Session %s is inactive" % session.name)
					send_msg(ws, {'status': 'inactive', 'session_name': session.name}, type=cmd)
				continue
					
			if cmd == 'sniffupdate':
				data = session.update_nodes()
				data['type'] = cmd
				if data:
					ws.send(dumps(data))
				continue

			if cmd == 'flowstatus':
				data = session.flow_status()
				data['type'] = cmd
				if data:
					ws.send(dumps(data))
				continue

			if cmd == 'get_flow_payload':
				fid = message['flowid']
				flow = session.flows[fid]
				data = {}
				data['payload'] = flow.get_payload('web')
				data['type'] = cmd
				ws.send(dumps(data))
				continue
		
	return ""



# test functions

def echo(ws):
	while True:
			message = ws.receive()
			ws.send(message)

if __name__ == "__main__":
	

	# options
	parser = argparse.ArgumentParser(description="Malcom - malware communications analyzer")
	parser.add_argument("-i", "--interface", help="Listen interface", default=app.config['LISTEN_INTERFACE'])
	parser.add_argument("-p", "--port", help="Listen port", type=int, default=app.config['LISTEN_PORT'])
	parser.add_argument("-f", "--feeds", help="Run feeds (use -ff to force run on all feeds)", action="count")
	parser.add_argument("-t", "--max-threads", help="Number of threads to use (default 4)", type=int, default=app.config['MAX_THREADS'])
	parser.add_argument("--public", help="Run a public instance (Feeds and network sniffing disabled)", action="store_true", default=app.config['PUBLIC'])
	#parser.add_argument("--no-feeds", help="Disable automatic feeding", action="store_true", default=app.config['NO_FEED'])
	args = parser.parse_args()

	
	os.system('clear')
	app.config['LISTEN_INTERFACE'] = args.interface
	app.config['LISTEN_PORT'] = args.port
	app.config['MAX_THREADS'] = args.max_threads
	app.config['PUBLIC'] = args.public

	analytics_engine.max_threads = threading.Semaphore(app.config['MAX_THREADS'])

	sys.stderr.write("===== Malcom %s - Malware Communications Analyzer =====\n\n" % app.config['VERSION'])
	sys.stderr.write("Starting server in %s mode...\n" % ("public" if app.config['PUBLIC'] else "private"))
	sys.stderr.write("Detected interfaces:\n")
	for i in [i for i in ni.interfaces() if i.find('eth') != -1]:
		sys.stderr.write("%s:\t%s\n" % (i, ni.ifaddresses(i).get(2,[{'addr':'Not defined'}])[0]['addr']))


	sys.stderr.write("Importing feeds...\n")
	feed_engine.load_feeds()


	# call malcom to run feeds - this will not start the web interface
	if args.feeds >= 1:
		if args.feeds == 1:
			feed_engine.start()
			sys.stderr.write("Starting feed scheduler...\n")
			cmd = raw_input()
			while cmd != 'exit':
				try:
					sys.stderr.write('Command entered %s\n' % cmd)
					cmd = raw_input()
				except KeyboardInterrupt:
					sys.stderr.write(" caught: Exiting gracefully\n")
					feed_engine.stop_all_feeds()
					exit(0)
				
		elif args.feeds == 2:
			feed_engine.run_all_feeds()
		
		exit(0)


	sys.stderr.write("Web interface running on %s:%s with %s maximum threads\n\n" % (app.config['LISTEN_INTERFACE'], app.config['LISTEN_PORT'], app.config["MAX_THREADS"]))

	try:
		http_server = WSGIServer((app.config['LISTEN_INTERFACE'], app.config['LISTEN_PORT']), malcom_app, handler_class=WebSocketHandler)
		http_server.serve_forever()
	except KeyboardInterrupt:

		sys.stderr.write(" caught: Exiting gracefully\n")

		if len(sniffer_sessions) > 0:
			debug_output('Stopping sniffing sessions...')
			for s in sniffer_sessions:
				sniffer_sessions[s].stop()

		feed_engine.stop_all_feeds()
		exit(0)
