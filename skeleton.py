#!/usr/bin/python
import threading
import queue
import socket

import signal
import sys

from app import app

#global declarations and variables
_running = 0 #overall program status
_connected = 0 #connection to stream status
_q = queue.Queue() #queue for data stream
_qlock = threading.Lock() #mutex lock for queue
_threads = []
_threadID = 1

#connect to data stream
def init_stream():
	host = "cs261.dcs.warwick.ac.uk"
	host_port = 80
	connect_stream()
	netcat(host, host_port)

def connect_stream():
	global _connected 
	_connected = 1

#disconnects from data stream
def disconnect_stream():
	global _connected 
	_connected = 0

#connects to host, port
def netcat(host, port):
	try:
		#initialises socket
		s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	except socket.error:
		print("Failed to create socket")

	s.connect((host, int(port)))
	#TODO: filter out first line, first line is always [time,buyer,seller,price,size,currency,symbol,sector,bid,ask]
	
	global _running
	global _connected
	global _q
	while(_running):
		while (_connected):
			data = s.recv(4096); #TODO: rework numbers
			if(len(data)>0):
				#puts new line of data into queue
				#print(data)
				_qlock.acquire()
				_q.put(data)
				_qlock.release()
	s.close()

#threading
class streamThread (threading.Thread):
	def __init__(self, threadID):
		threading.Thread.__init__(self)
		self.threadID = threadID
	def run(self):
		print("Starting stream thread")
		init_stream()

class handlerThread (threading.Thread):
	def __init__(self, threadID):
		threading.Thread.__init__(self)
		self.threadID = threadID
	def run(self):
		print("Starting event listener thread")
		eventListener()

def eventListener():
	global _running
	global _qlock
	while(_running):
		#detects inputs
		try:
			#TODO actually link up mechanism to connect/disconnect
			var = input()
			if(var=='disconnect'):
				disconnect_stream()
				_qlock.acquire()
				print("Current queue size: " + str(_q.qsize()))
				_qlock.release()
			if(var=='connect'):
				connect_stream()
		except:
			break

class processorThread (threading.Thread):
	def __init__(self, threadID):
		threading.Thread.__init__(self)
		self.threadID = threadID
	def run(self):
		print("Starting processing thread")
		processing()

def processing():
	global _qlock
	global _running
	#todo remove not
	while(not _running):
		_qlock.acquire()
		if(_q.qsize()>0):
			data = _q.get()
		_qlock.release()
		#todo processing here

def getdata():
	global _q
	data = _q.get()
	return data

#signal handling to terminate/quit
def signal_handler(signal, frame):
	global _running
	global _threads
	
	#set running states
	disconnect_stream()
	_running=0
	#rejoin threads
	for t in _threads:
		#print(t.isAlive())
		t.join()
		#print(t.isAlive())
	sys.exit()

#############################
#			main			#
#############################
@app.route('/refresh', methods=['POST'])
def refresh():
	return getdata()

#run web server on main thread
if __name__ == '__main__':
	_running = 1

	#create threads
	thread1 = streamThread(_threadID)
	processor = processorThread(_threadID)
	handler = handlerThread(_threadID)
	handler.daemon = True

	thread1.start()
	_threads.append(thread1)
	_threadID += 1

	processor.start()
	_threads.append(processor)
	_threadID += 1

	handler.start()
	_threads.append(handler)
	_threadID += 1

	#signal handler
	signal.signal(signal.SIGINT, signal_handler)
	app.run()