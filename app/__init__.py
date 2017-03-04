#!/usr/bin/python

#front end imports
from flask import Flask
from flask import request, session, redirect, url_for
from werkzeug.utils import secure_filename
import json
import logging
import atexit

#processing
from datetime import datetime
import numpy as np

#modules by us
from app import mtrade
from app import database
from app import detection

#misc
import uuid #generating random session ids
import threading
import queue
import socket, errno
import signal
import sys
import time #for testing, may need for timed reconnection
import os
import csv

#global declarations and variables
_mode = 1 #1 live, 0 = static
_running = 0 #overall program status
_connected = 0 #connection to stream status
_staticlive = 0 #1 if a file has been uploaded
_q = queue.Queue() #queue for data stream
_qlock = threading.Lock() #mutex lock for queue
_threads = []
_threadID = 1

_anomalycounter = 0
_tradecounter = 0
_tradecounterlock = threading.Lock()
_tradevalue = 0

_sessions = {} #associative array/dictionary to store all the instances of ui
_sessionslock = threading.Lock()

def init_app():
	app = Flask(__name__)
	global _running
	global _threads
	
	def interrupt():
		global _running
		global _threads
		#set running states
		disconnect_stream()
		_running=0
		#rejoin threads
		for t in _threads:
			#print("Thread " + t.name + " is alive: " + str(t.isAlive()))
			t.join()
			#print("Thread " + t.name + " is alive: " + str(t.isAlive()))
	
	def signal_handler(signal, frame):
		interrupt()
		sys.exit(0)
	#flask configuration
	#for sessions, generate a key instead
	
	_running=1
	#app configurations
	app.secret_key = str(uuid.uuid4())
	app.config['UPLOAD_FOLDER'] = ''
	
	load_data()
	init_threads()
	
	#on exit
	atexit.register(interrupt)
	#signal handler
	signal.signal(signal.SIGINT, signal_handler)
	return app

def load_data():
	#loads data from db
	global _tradevalue
	global _tradecounter
	global _anomalycounter

	db = database.Database()
	_tradecounter = int(db.tradecount())
	_anomalycounter = int(db.anomalycount())
	_tradevalue = db.tradevalue()

def init_threads():
	#create threads
	global _threads
	global _threadID
	tstream = StreamThread(_threadID)
	tprocessor = ProcessorThread(_threadID)
	thandler = HandlerThread(_threadID)
	thandler.daemon = True
	
	tstream.start()
	_threads.append(tstream)
	_threadID += 1
	
	tprocessor.start()
	_threads.append(tprocessor)
	_threadID += 1
	
	thandler.start()
	_threads.append(thandler)
	_threadID += 1

def connect_stream():
	global _connected
	_connected = 1

#disconnects from data stream
def disconnect_stream():
	global _connected
	_connected = 0

class StreamThread(threading.Thread):
	def __init__(self, threadID):
		threading.Thread.__init__(self)
		self.threadID = threadID
		self.name = "Data stream"
	def run(self):
		print("Starting stream thread")
		self.manage_stream()
	
	#connect to data stream
	def init_stream(self):
		host = "cs261.dcs.warwick.ac.uk"
		host_port = 80
		connect_stream()
		self.netcat(host, host_port)
	
	def manage_stream(self):
		global _running
		global _connected
		self.init_stream() #stream is a blocking method
		#reconnects if commanded
		while(_running):
			if(_connected):
				self.init_stream()

	#connects to host, port
	def netcat(self, host, port):
		try:
			#initialises socket
			s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		except socket.error:
			print("Failed to create socket")
		
		try:
			s.connect((host, int(port)))
			print("Stream connected")
			
			global _running
			global _connected
			global _q
			global _qlock
			#TODO: filter out first line, first line is always [time,buyer,seller,price,size,currency,symbol,sector,bid,ask]
			data = s.recv(4096)
			counter=0 #a counter to see how many iterations of no data
			while (_connected):
				s.settimeout(2) #if no data comes in, s.recv becomes blocking
				try:
					data = s.recv(4096) #TODO: rework numbers
				except socket.timeout as e:
					if(counter>60):
						print("No data received in the past 2 minutes. Disconnecting stream")
						#raise error
						disconnect_stream()
					counter+=1
				else:
					if(len(data)>0):
						#puts new line of data into queue
						if(_staticlive == 0):	
							_qlock.acquire()
							_q.put(data)
							_qlock.release()
						elif(_uploaddone == 1):
							while not(_q.empty()):
								_qlock.acquire()
								_q.put(mtrade.to_TradeData(row))
								_qlock.release()
							_uploaddone = 0
					counter=0
			
			s.close()
			print("\nStream disconnected")
		
		except socket.error as e:
			#debugging midnight error
			print(str(e.errno))
			#connecting if stream is down error
			if e.errno == errno.ECONNREFUSED:
				print("Stream down, aborting. Please manually reconnect")
				disconnect_stream()

class HandlerThread (threading.Thread):
	def __init__(self, threadID):
		threading.Thread.__init__(self)
		self.threadID = threadID
		self.name = "Handler"
	def run(self):
		print("Starting event listener thread")
		self.eventListener()
	
	def eventListener(self):
		global _running
		global _qlock
		global _sessions
		while(_running):
			#detects inputs, remove when complete
			debug=0
			if(debug==1):
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
			#detect timers
			#TODO disconnect stream and reconnect
			#automatically reconnect
			global _mode
			global _connected
			if(_mode==1 and _connected==0):
				connect_stream()
			
			#TODO loop through sessions
			for key in list(_sessions): #create a copy of list as size will change due to deletion
				#see when it was last accessed, if it hasn't been accessed in the past 6 mins, delete it
				session_time = str(_sessions[key].lastAccess)
				now = str(datetime.now())
				FMT = "%Y-%m-%d %H:%M:%S.%f"
				difference = datetime.strptime(now, FMT).timestamp() - datetime.strptime(session_time, FMT).timestamp()
				#print(difference)
				if(difference>=300):
					#delete session
					global _sessionslock
					_sessionslock.acquire()
					del _sessions[key]
					_sessionslock.release()
					#print("Session ID: " + str(key) + " deleted")
			
			for i in range(30):
				if(_running):
					time.sleep(1) #wait 30 secs, no point doing every second
				else:
					break
class StockData:
	#contains company symbol, polynomial coefficients for best fit line and range within it's considered not anomolalous
	def __init__(self, symbol, stepNumOfStepsPairs):
		self.symbol = symbol
		self.priceRegression = PriceRegression(_numberOfRegressors)
		self.volumeRegression = VolumeRegression(0, stepNumOfStepsPairs[0][1])
		self.frequencyRegression = AvgOverTimeRegression(0, stepNumOfStepsPairs[0][1])

#Should make it more expandable/less messy
class PriceRegression:
	def __init__(self, numOfRegressors):
		self.numOfRegressors = numOfRegressors
		self.xVals = np.empty(numOfRegressors)
		self.yVals = np.empty(numOfRegressors)
		self.currCnt = 0
		self.rangeVal = 0.4 #to be adjusted
		self.coeffList = [0.0, 0.0]
	
	#compare actual vs predicted value
	def detectError(self, x, y):
		return (y>=(x*self.coeffList[0]+self.coeffList[1])*(1+self.rangeVal) or
				y<=(x*self.coeffList[0]+self.coeffList[1])*(1-self.rangeVal))
	
	def updateCoeffs(self):
		self.coeffList = np.polyfit(self.xVals, self.yVals, 1)

#Should make it more expandable/less messy
class AvgOverTimeRegression:
	def __init__(self, stepNumOfStepsPairsIndex, numOfSteps):
		# self.stepNumOfStepsPairsIndex = stepNumOfStepsPairsIndex #for better encapsulation in the future
		self.xVals = np.zeros(numOfSteps)
		self.yVals = np.zeros(numOfSteps)
		self.tempXVals = 0
		self.rangeVal = 0.5 #changed for testing
		self.coeffList = [0.0, 0.0]
	
	# compare actual vs predicted value
	def detectError(self, x, y):
		return (y>=(x*self.coeffList[0]+self.coeffList[1])*(1+self.rangeVal) or
				y<=(x*self.coeffList[0]+self.coeffList[1])*(1-self.rangeVal))
	
	def updateCoeffs(self):
		self.coeffList = np.polyfit(self.xVals, self.yVals, 1)

class VolumeRegression(AvgOverTimeRegression):
	def __init__(self, stepNumOfStepsPairsIndex, numOfSteps):
		super().__init__(stepNumOfStepsPairsIndex, numOfSteps)
		self.tempXVals = []

class ProcessorThread(threading.Thread):

	stepNumOfStepsPairs = [[20, 6]]
	tickTimeCntPairs = [[0,0]]

	senstivityPerTrader = 5

	def __init__(self, threadID):
		threading.Thread.__init__(self)
		self.threadID = threadID
		self.name = "Data processor"
	def run(self):
		print("Starting processing thread")
		
		#setup company data, one-off at the beginning
		global companyList
		companyList = {}
		#some process to load data from db
		global _numOfStepVariants
		_numOfStepVariants = len(self.stepNumOfStepsPairs)

		#rolling average for all traders
		#(exponential moving average should make it very computationally efficient)
		global traderList
		traderList = {}
		
		self.processing() #currently doing live data
	
	
	def setupCompanyData(self,t):
		companyList[t.symbol] = StockData(t.symbol, self.stepNumOfStepsPairs)
		for x in range(_numOfStepVariants):
			self.tickTimeCntPairs[x][0] = (int(self.timeToInt(t.time)/self.stepNumOfStepsPairs[x][0]))*self.stepNumOfStepsPairs[x][0] #rounds down to to the nearest step

	#print(trade.symbol, " setup") #debugging
	
	
	def timeToInt(self,time):
		val = 0
		try:
			val = datetime.strptime(time, "%Y-%m-%d %H:%M:%S.%f").timestamp()
		except ValueError:
			val = datetime.strptime(time, "%Y-%m-%d %H:%M:%S").timestamp()
		return val
	
	global _numberOfRegressors
	_numberOfRegressors = 10
	
	def new_anomaly(self,db,tradeid,t,category):
		global _sessionslock
		global _anomalycounter
		global _sessions
		anomalyid = -1
		anomalyid = db.addAnomaly(tradeid, category)
		newAnomaly = mtrade.Anomaly(anomalyid, t, category) #todo change 3
		#doSomething with the anomaly
		_anomalycounter+=1
			#for each key in session, add this anomaly
		_sessionslock.acquire
		for key in _sessions:
			_sessions[key].put(newAnomaly)
		_sessionslock.release
	
	def processing(self):
		#state is processing static/live
		global _qlock
		global _running
		global _q
		#connect to db
		db = database.Database()
		while(_running):
			trades=self.dequeue(_q,_qlock)
			for t in trades:
				#update counts
				global _tradecounter
				global _tradecounterlock
				global _anomalycounter
				global _tradevalue
				
				#######
				#anomalies
				trade_anomaly = []
		
				_tradecounter+=1 #TODO move elsewhere and mutex lock
				_tradevalue+=float(t.price)*float(t.size)
				#trade is in TradeData format (see trade.py)
				
				symb = t.symbol
				#dump to db
				tradeid = db.addTransaction(t)
				if(tradeid==-1):
					#error has occurred TODO insert better handler
					print("Error adding trade")
				
				#create a StockData object for every company
				if (symb not in companyList):
					self.setupCompanyData(t)
				
				# print(symb, timeToInt(trade.time), trade.price) #debugging


				#
				#	PRICE REGRESSION
				#

				#update keep a buffer of _num_of_regressors recent values for regression
				companyList[symb].priceRegression.xVals[companyList[symb].priceRegression.currCnt] = self.timeToInt(t.time)
				companyList[symb].priceRegression.yVals[companyList[symb].priceRegression.currCnt] = t.price
				
				companyList[symb].priceRegression.currCnt += 1
				# print(companyList[symb].currCnt) #debugging			
			
				#every several values, the line fit is updated (prevents constant updates)
				if(companyList[symb].priceRegression.currCnt == companyList[symb].priceRegression.numOfRegressors):
					companyList[symb].priceRegression.updateCoeffs()
					# print (companyList[symb].priceCoeffList) #debugging
					companyList[symb].priceRegression.currCnt = 0


				# PRICE ANOMALY DETECTION
				if(np.all(companyList[symb].priceRegression.coeffList != [0.0, 0.0])):
					# print(companyList[symb].priceCoeffList) #debugging
					if(companyList[symb].priceRegression.detectError(self.timeToInt(t.time), float(t.price))):
						print("price anomaly") #debugging
						#add price anomaly to category
						trade_anomaly.append(1)

				#
				#	VOLUME REGRESSION
				#


				for x in range(len(self.stepNumOfStepsPairs)): #for every possible tick length/beginning time
					if(self.timeToInt(t.time) >= self.stepNumOfStepsPairs[x][0]+self.tickTimeCntPairs[x][0]): #if the tick has finished
						if(self.tickTimeCntPairs[x][1] >= self.stepNumOfStepsPairs[x][1]):	#if the number of ticks exceeded maximum (i.e. it's time to upadte the line fit)
							self.tickTimeCntPairs[x][1] = 0
							self.tickTimeCntPairs[x][0] += self.stepNumOfStepsPairs[x][0]
							for company in companyList.values():	#every tick, sum the value of voulmes in that step and store, update current step start time and step count
								if (company.volumeRegression.detectError(self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2), sum(company.volumeRegression.tempXVals))
									and np.all(company.volumeRegression.coeffList > [0.0, 0.0])):
									trade_anomaly.append(2)
									#print("volume anomaly for x=", sum(company.volumeRegression.tempXVals), " y=", self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)) #debugging
									#print("expected x=", company.volumeRegression.coeffList[0]*self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)+(self.stepNumOfStepsPairs[x][0]/2), " +/- ", company.volumeRegression.rangeVal) #debugging
								
								if(np.all(company.volumeRegression.coeffList != [0.0, 0.0])): #on second (first guaranteed completed) and subsequent passes
									company.volumeRegression.updateCoeffs()
								else:
									company.volumeRegression.coeffList = [-1.0, -1.0]	#mark the beginning of a first complete pass


								company.volumeRegression.xVals[self.tickTimeCntPairs[x][1]] = sum(company.volumeRegression.tempXVals)
								company.volumeRegression.yVals[self.tickTimeCntPairs[x][1]] = self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)

						else:							
							for company in companyList.values():	#every tick, sum the value of voulmes in that step and store, update current step start time and step count
								# print(self.tickTimeCntPairs[x][1], self.stepNumOfStepsPairs[x][1])
								if (company.volumeRegression.detectError(self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2), sum(company.volumeRegression.tempXVals))
									and np.all(company.volumeRegression.coeffList > [0.0, 0.0])):
									#print("volume anomaly for x=", sum(company.volumeRegression.tempXVals), " y=", self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)) #debugging
									#print("expected x=", company.volumeRegression.coeffList[0]*self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)+(self.stepNumOfStepsPairs[x][0]/2), " +/- ", company.volumeRegression.rangeVal) #debugging
									trade_anomaly.append(2)
								company.volumeRegression.xVals[self.tickTimeCntPairs[x][1]] = sum(company.volumeRegression.tempXVals) #TODO what happens where no trade comes in during the whole tick
								company.volumeRegression.yVals[self.tickTimeCntPairs[x][1]] = self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)
								# print(self.tickTimeCntPairs[x][1])
								# print(self.tickTimeCntPairs[x][0])

							self.tickTimeCntPairs[x][1] += 1
							#print(self.tickTimeCntPairs[x][1])
							self.tickTimeCntPairs[x][0] += self.stepNumOfStepsPairs[x][0]



				companyList[symb].volumeRegression.tempXVals.append(float(t.size))


				#
				#	FREQUENCY REGRESSION
				#

				for x in range(len(self.stepNumOfStepsPairs)): #for every possible tick length/beginning time
					if(self.timeToInt(t.time) >= self.stepNumOfStepsPairs[x][0]+self.tickTimeCntPairs[x][0]): #if the tick has finished
						if(self.tickTimeCntPairs[x][1] >= self.stepNumOfStepsPairs[x][1]):	#if the number of ticks exceeded maximum (i.e. it's time to upadte the line fit)
							self.tickTimeCntPairs[x][1] = 0
							self.tickTimeCntPairs[x][0] += self.stepNumOfStepsPairs[x][0]
							for company in companyList.values():	#every tick, sum the value of voulmes in that step and store, update current step start time and step count
								if (company.frequencyRegression.detectError(self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2), company.frequencyRegression.tempXVals)
									and np.all(company.frequencyRegression.coeffList > [0.0, 0.0])):
									print("frequency anomaly for x=", company.frequencyRegression.tempXVals, " y=", self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)) #debugging
									print("expected x=", companyList[symb].frequencyRegression.coeffList[0]*self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)+(self.stepNumOfStepsPairs[x][0]/2), " +/- ", companyList[symb].frequencyRegression.rangeVal) #debugging
						
								if(np.all(company.frequencyRegression.coeffList != [0.0, 0.0])): #on second (first guaranteed completed) and subsequent passes
									company.frequencyRegression.updateCoeffs()
								else:
									company.frequencyRegression.coeffList = [-1.0, -1.0]	#mark the beginning of a first complete pass


								company.frequencyRegression.xVals[self.tickTimeCntPairs[x][1]] = company.frequencyRegression.tempXVals
								company.frequencyRegression.yVals[self.tickTimeCntPairs[x][1]] = self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)

						else:							
							for company in companyList.values():	#every tick, sum the value of voulmes in that step and store, update current step start time and step count
								# print(self.tickTimeCntPairs[x][1], self.stepNumOfStepsPairs[x][1])
								if (company.frequencyRegression.detectError(self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2), company.frequencyRegression.tempXVals)
									and np.all(company.frequencyRegression.coeffList > [0.0, 0.0])):
									print("frequency anomaly for x=", company.frequencyRegression.tempXVals, " y=", self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)) #debugging
									print("expected x=", companyList[symb].frequencyRegression.coeffList[0]*self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)+(self.stepNumOfStepsPairs[x][0]/2), " +/- ", companyList[symb].frequencyRegression.rangeVal) #debugging
						
								company.frequencyRegression.xVals[self.tickTimeCntPairs[x][1]] = company.frequencyRegression.tempXVals #TODO what happens where no trade comes in during the whole tick
								company.frequencyRegression.yVals[self.tickTimeCntPairs[x][1]] = self.tickTimeCntPairs[x][0]+(self.stepNumOfStepsPairs[x][0]/2)
								# print(self.tickTimeCntPairs[x][1])
								# print(self.tickTimeCntPairs[x][0])

							self.tickTimeCntPairs[x][1] += 1
							#print(self.tickTimeCntPairs[x][1])
							self.tickTimeCntPairs[x][0] += self.stepNumOfStepsPairs[x][0]



				companyList[symb].frequencyRegression.tempXVals += 1 #might delete that variable in the future

				#
				#	ANOMALY BY TRADER
				#

				if(t.seller not in traderList):
					traderList[t.seller] = float(t.size)*float(t.price)
				else:
					traderList[t.seller] = traderList[t.seller]*0.9 + 0.1*float(t.size)*float(t.price)
					if(#traderList[t.seller] > float(t.size)*float(t.price)*self.senstivityPerTrader or
						traderList[t.seller] < float(t.size)*float(t.price)/self.senstivityPerTrader):
						print("trader val error", float(t.size)*float(t.price), "expected ", traderList[t.seller])
						trade_anomaly.append(3)
				
				#categorising and adding anomalies
				#key
				#1 price spike/trough
				#2 volume spike/trough
				#3 suspicious trader activity
				#calculate category
				if(len(trade_anomaly)>0):
					#add anomaly to db
					cat = -1
					if((1 in trade_anomaly) & (2 in trade_anomaly)):
						###possibly pump and dump?
						a=1
					if((2 in trade_anomaly) & (3 in trade_anomaly)):
						###insider information/bear raids?
						a=1
					if(1 in trade_anomaly):
						a=1
					self.new_anomaly(db,tradeid,t,cat)

		
		#time.sleep(2) #REMOVE AFTER TESTING, to slow down processing
		time.sleep(0.01) #good for cpu
		db.close()

	def dequeue(self,q,qlock):
		trades=[]
		data = ""
		qlock.acquire()
		if(q.qsize()>0):
			data = q.get()
		qlock.release()
		if(len(data)>0): #TODO do a better check
			#converts byte to string
			data = str(data.decode("utf-8"))
			data = data[:-2] #removes \r\n at the end, TODO what if it doesn't have \r\n?
			data = data.split('\n') #gets a list where each element is a new trade
			for x in data:
				try:
					t = mtrade.parse(x)
					trades.append(t)
				except IndexError:
					print("index error") #TODO work out why it does this
		return trades

#session stuff
class SessionData():
	def __init__(self,id):
		self.id = id
		self.queue = queue.Queue() #queue for anomalies
		self.qlock = threading.Lock()
		self.lastAccess = datetime.now()
	
	def lock(self):
		self.qlock.acquire()
	def release(self):
		self.qlock.release()
	def put(self,data):
		self.lock()
		self.queue.put(data)
		self.release()
	def get(self):
		self.lock()
		data = self.queue.get()
		self.release()
		return data
	def empty(self):
		self.updateaccess()
		self.lock()
		empty = self.queue.empty()
		self.release()
		return empty
	def updateaccess(self):
		self.lastAccess = datetime.now()

#signal handling to terminate/quit
def signal_handler(signal, frame):
	global _running
	global _threads
	
	#set running states
	disconnect_stream()
	_running=0
	#rejoin threads
	for t in _threads:
		#print("Thread " + t.name + " is alive: " + str(t.isAlive()))
		t.join()
	#print(t.isAlive())
	sys.exit()

def getdata():
	#this is for the flask application, gets data for front end
	global _connected
	global _anomalycounter
	global _tradecounter
	global _tradevalue
	global _mode
	
	connected = False
	if(_connected==1):
		connected = True

	#puts into json format
	data = {}
	data["mode"] = _mode
	data["live"] = connected
	data["anomaly"] = _anomalycounter
	data["trades"] = _tradecounter
	data["tradevalue"] = format(_tradevalue, '.2f')

	#empty anomaly queue
	anomalies = []
	if(session.get('id') is not None):
		#print(session['id']) #debugging
		try:
			sessiondata = _sessions[session['id']]
			while not sessiondata.empty():
				x = sessiondata.get()
				#TODO
				temp = x.trade.time.split()
				anomaly = {}
				anomaly['id'] = x.id
				anomaly['type'] = x.category
				anomaly['date'] = temp[0]
				anomaly['time'] = temp[1]
				anomaly['action'] = x.trade.symbol
				anomalies.append(anomaly)
		except KeyError:
			# Key is not present, no sessions for user
			#TODO insert better handler, tell user to refresh?
			pass
	else:
		#add session
		init_session()

	data["anomalies"] = anomalies
	return json.dumps(data)

#############
app=init_app()
#view stuff here
from app import views
##########AJAX ROUTING requests###########
@app.route('/refresh', methods=['POST'])
def refresh():
	return getdata()

@app.route('/refresh_anomaly', methods=['POST'])
def refresh_anomaly():
	global _mode
	global _connected
	data = {}
	data["mode"] = _mode
	data["live"] = _connected
	return json.dumps(data)

#toggling between live and static
@app.route('/toggle', methods=['POST'])
def toggle():
	mode = int(request.json['mode'])
	global _mode
	global _connected
	if (mode==0):
		if(_connected==1):
			disconnect_stream()
		_mode=0
	if (mode==1):
		if(_connected!=1):
			connect_stream()
		else:
			return json.dumps({"change":False})
		_mode=1
	return json.dumps({"change":True})

#connect/disconnect
@app.route('/connect', methods=['POST'])
def toggleconnect():
	global _mode
	global _connected
	if(_mode==1):
		if(_connected==1):
			disconnect_stream()
			return json.dumps({"change":True})
		if(_connected==0):
			connect_stream()
			return json.dumps({"change":True})
	return json.dumps({"change":False})

@app.route('/reset', methods=['POST'])
def resetstats():
	#for resetting current stats and db?
	db = database.Database()
	success=db.clearall()
	global _tradevalue
	global _tradecounter
	global _anomalycounter
	
	if(success):
		_tradevalue = 0
		_tradecounter = 0
		_anomalycounter = 0
		return "ok"
	return "fail"
	
@app.route('/session', methods=['POST'])
def init_session():
	#check to see if a session has already been established
	id = uuid.uuid4()
	session['id'] = id
	sessiondata = SessionData(id)
	_sessions[id] = sessiondata
	return "ok"

def prepare():
		#needs to clear current queue and reset counters
		global _tradecounter
		global _anomalycounter
		global _tradevalue
		global _mode
		_mode = 0
		#disconnect_stream()
		_tradecounter = 0
		_anomalycounter = 0
		_tradevalue = 0

def parsefile(file):
		#read file
		global _qlock
		global _q
		global _staticlive

		_staticlive = 1
		prepare()

		with open(file, 'r') as csvfile:
			
			reader = csv.reader(csvfile, delimiter = ',')

			while not _q.empty():
			    try:
			    	_q.get()
			    except Empty:
			        continue

			for row in reader:
				if row[0] == 'time':
					continue
				else:
					print(row[0], row[1], row[2], row[3],row[4], row[5], row[6], row[7], row[8], row[9])
					#time.sleep(0.5)
			_uploaddone = 1

		_staticlive = 0
		#connect_stream()

ALLOWED_EXTENSIONS = set(['csv'])

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/upload', methods=['GET', 'POST'])
def upload_file():
	
	if request.method == 'POST':
		f = request.files['file']
		
		if f.filename == '':
	            flash('No selected file')
	            return "not ok"

		if f and allowed_file(f.filename):
			
			print("Reading File \n#####\n#####\n#####")
			filename = secure_filename(f.filename)
			
			f.save(os.path.join(app.config['UPLOAD_FOLDER'], "traders.csv"))
			
			#DO NOT DO PROCESSING IN THIS MAIN THREAD@@@@@ NOT EVEN READINGtet
			#f = parsefile(filename)
			
			#create another thread to put into queue otherwise will be blocking/delay in return
			#it should also empty contents of old queue

		else:
			print("Error with file upload")
			return "not ok"
	return "ok"

@app.route('/getanomalies', methods=['POST'])
def init_data():
	#get data in db
	db = database.Database()
	a = db.getAnomalies(0)
	db.close()
	data = {}
	#TODO
	anomalies = []
	for x in a:
		temp = x.trade.time.split()
		anomaly = {}
		anomaly['id'] = x.id
		anomaly['type'] = x.category
		anomaly['date'] = temp[0]
		anomaly['time'] = temp[1]
		anomaly['action'] = x.trade.symbol
		anomalies.append(anomaly)
	#make into json
	data["anomalies"] = anomalies
	return json.dumps(data)
