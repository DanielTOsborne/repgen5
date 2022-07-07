import pytz,datetime,sys
import operator
from inspect import isfunction
import copy
import math
from decimal import Decimal,DivisionByZero,DecimalException,getcontext
from ssl import SSLError
import re
from repgen.util import extra_operator
import signal

try:
	# Relativedelta supports months and years, but is external library
	from dateutil.relativedelta import relativedelta as timedelta
except:
	# Included with python, but doesn't support longer granularity than weeks.
	# This can cause issues for leap years if not accounted for.
	from datetime import timedelta

# types
string_types = ("".__class__,u"".__class__)
number_types = (int,float,complex,Decimal)

def handler(signum, frame):
	sys.stderr.write(f'Signal handler called with signal ${signum}\n')
	raise TimeoutError('Timeout opening socket connection.')

# Failsafe for timeout
if sys.platform != "win32":
	signal.signal(signal.SIGALRM, handler)

class Value:
	shared = {
		"picture" : "NNZ",
		"misstr"  : "-M-",
		"undef"   : "-?-",
		"missdta"  : -901,
		"missing": "MISSOK",  # How to treat missing values

		# shared and updated between calls
		"host" : None,        # ip address/hostname or file name
		"dbtype" : None,      # file or spkjson
		"query": None,
		"tz" : pytz.utc,
		"start": None,
		"end": None,
		"interval": None,
		"value": None,        # this value is only used for generating time series
		"timeout": None,      # socket (http/ssl) timeout
	}

	#region Properties
	def __get_time(self):
		if self.start == self.end: return self.start
		else: return None
	def __set_time(self, value: datetime.datetime):
		self.start = self.end = value

	# 'time' can only be set if 'start' and 'end' are the same value, so just wrap it in a property.
	time = property(__get_time, __set_time)	# type: datetime.datetime
	#endregion

	def __init__( self, *args, **kwargs ):
		def processDateTime(value, date_key, time_key):
			if isinstance(value, str) or isinstance(value, int):
				is_24 = False

				if str(value).startswith("2400"):
					is_24 = True
					value = str(value).replace("2400", "0000", 1)

				# Datetime
				for fmt in ["%H%M %d%m%Y", "%H%M %d%m%y"]:
					try:
						value = datetime.datetime.strptime(str(value), fmt)
						break
					except ValueError: pass

				# Date only
				for fmt in ["%d%m%Y", "%d%m%y"]:
					try:
						value = datetime.datetime.strptime(str(value), fmt).date()
						break
					except ValueError: pass

				# Time only
				for fmt in ["%H%M", "%H:%M"]:
					try:
						value = datetime.datetime.strptime(str(value), fmt).time()
						break
					except ValueError: pass

				Value.shared[key.lower()] = value
			return value

		self.index = None
		self.type="SCALAR"
		self.value = None
		self.values = []
		self.picture="%s"

		if len(args) == 1 and isinstance(args[0], Value):
			# This is emulating a "copy constructor" which does a deep copy.
			value = copy.deepcopy(args[0])
			self.index = value.index
			self.type = value.type
			self.value = value.value
			self.values = value.values
			self.picture = value.picture
			self.dbtype = value.dbtype
			self.query = value.query
			self.missdta = value.missdta
			self.missing = value.missing

			if value.type == "SCALAR" and isinstance(value.value, Value):
				self.value = value.value.value
			# If times were passed in as a method result, they might in a Value object
			if isinstance(value.start, Value):
				value.start = value.start.value
			if isinstance(value.end, Value):
				value.end = value.end.value

			self.start = value.start
			self.end = value.end
			self.__dict__ = value.__dict__

		# go through the keyword args,
		# set them as static variables for the next call

		# update the shared keywords
		for key in kwargs:
			value = kwargs[key]
			# If the value is wrapped in quotes, it's most likely wrong (possibly value was read from a file where it had quotes).
			if isinstance(value, str) and len(value) > 0 and value[0] == '"' and value[-1] == '"':
				value = value[1:-1]

			if key.lower()=="tz" and isinstance(value, string_types):
				value = pytz.timezone(value)
			if (key.lower() == "start" or key.lower() == "end" or key.lower().endswith("time") or key.lower().endswith("date")):
				if isinstance(value,(Value)):
					if value.type == 'TIMESERIES' and len(value.values) == 1:
						value = value.values[0][0]
					else:
						value = value.value # internally we want the actual datetime

				value = processDateTime(value, key[0:-4] + "date", key[0:-4] + "time")

				if key.lower().startswith('s'):
					Value.shared["start"] = value
				elif key.lower().startswith('e'):
					Value.shared["end"] = value
				else:
					Value.shared["start"] = value
					Value.shared["end"] = value

			if key.lower() == "value":
				key = "missing"

			Value.shared[key.lower()] = value

		# Correct any split date/times
		if not isinstance(Value.shared["start"], datetime.datetime):
			date = None
			time = None

			if "sdate" in Value.shared: date = Value.shared["sdate"]
			elif "date" in Value.shared: date = Value.shared["date"]
			if "stime" in Value.shared: time = Value.shared["stime"]
			elif "time" in Value.shared: time = Value.shared["time"]

			if date and time:
				Value.shared["start"] = datetime.datetime.combine(date, time)

		if not isinstance(Value.shared["end"], datetime.datetime):
			date = None
			time = None

			if "edate" in Value.shared: date = Value.shared["edate"]
			elif "date" in Value.shared: date = Value.shared["date"]
			if "etime" in Value.shared: time = Value.shared["etime"]
			elif "time" in Value.shared: time = Value.shared["time"]
			if date and time:
				Value.shared["end"] = datetime.datetime.combine(date, time)

		# load the keywords for this instance
		if len(args) == 1 and isinstance(args[0], Value):
			for key in Value.shared:
				if not key in ["time", "start", "end", "value"]:
					self.__dict__[key] = Value.shared[key]
			return

		for key in Value.shared:
			if key != "value":
				self.__dict__[key] = Value.shared[key]

		if len( args ) == 1:
			if not isinstance(args[0], Value):
				self.value = args[0]
			if isinstance(args[0], list):
				self.type = "GROUP"
			return
		elif len(args)> 0: raise Exception ("Only 1 non named value is allowed")

		self.type = "TIMESERIES"
		self.values = [ ] # will be a tuple of (time stamp, value, quality )

		if self.dbtype is None:
			raise Exception("you must enter a scalar quantity if you aren't specifying a data source")
		elif self.dbtype.upper() == "FILE":
			pass
		elif self.dbtype.upper() == "COPY":
			pass
		elif self.dbtype.upper() == "GENTS":
			current_t = self.start
			end_t = self.end
			while current_t <= end_t:
				if isinstance(self.value, number_types):
					self.values.append( ( current_t.astimezone(self.tz),self.value,0 ) )
				elif isinstance(self.value, Value ):
					self.value = self.value.value
					self.values.append( ( current_t.astimezone(self.tz),self.value,0 ) )
				elif isfunction(self.value):
					self.values.append( ( current_t.astimezone(self.tz),self.value(),0 ) )

				current_t = current_t + self.interval
		elif self.dbtype.upper() == "TEXT":
			def parse_slice(value):
				# From: https://stackoverflow.com/a/54421070, modified to support repgen4 range format
				"""
				Parses a `slice()` from string, like `start:stop:step`. Older repgen4 syntax `start-end` is also supported.
				The `:` format is 0-based, the `-` format is 1-based (and end value is inclusive).
				"""
				old_format = False
				if ':' not in value: old_format = True

				if value:
					parts = re.split('-|:', value)
					if len(parts) == 1:
						# treat a single value as an explicit array index (start:start+1)
						parts = [parts[0], parts[0]]
					# else: slice(start, stop[, step])
				else:
					# slice()
					parts = []

				slc = [int(p) if p else None for p in parts]
				if old_format and slc[0]: slc[0] -= 1
				return slice(*slc)

			# This reads the specific scalar value from the specified file, at the specified line and columns
			self.type = "SCALAR"
			if not self.file:
				raise FileNotFoundError("dbtype TEXT specified, but no file to read from.")
			with open(self.file) as inp:
				lines = inp.readlines()
				line = "".join(lines[parse_slice(self.line if hasattr(self, 'line') else ':')])

				# COL hopefully isn't specified if multiple lines are
				if line.count('\n') > 1 and hasattr(self, 'col') and re.search(':|-', self.col):
					raise ValueError(f"LINE has a ranged argument '{self.line}', with COL specified '{self.col}'.")

				self.value = line[parse_slice(self.col if hasattr(self, 'col') else ':')]

				# Check to see if the data is numeric
				try: self.value = Decimal(self.value)		# Use Decimal to ensure the value is read exactly as it appears
				except DecimalException: pass
				
		elif self.dbtype.upper() == "SPKJSON":
			import json, http.client as httplib, urllib.parse as urllib

			fmt = "%d-%b-%Y %H%M"
			tz = self.tz
			units= self.dbunits
			ts_name = ".".join( (self.dbloc, self.dbpar, self.dbptyp, self.dbint, self.dbdur, self.dbver) )

			# Convert time to destination timezone
			start = self.start.astimezone(tz)
			end = self.end.astimezone(tz)

			sys.stderr.write("Getting %s from %s to %s in tz %s, with units %s\n" % (ts_name,start.strftime(fmt),end.strftime(fmt),str(tz),units))
			query = "/fcgi-bin/get_ts.py?"
			params = urllib.urlencode( {
				"site": ts_name,
				"units": units,
				"start_time": start.strftime(fmt),
				"end_time":   end.strftime(fmt),
				"tz": str(tz)
			})
			try:
				conn = httplib.HTTPConnection( self.host )
				conn.request("GET", query+params )
				print("Fetching: %s" % query+params)
				r1 = conn.getresponse()
				data =r1.read()

				data_dict = json.loads(data)
				# get the depth
				prev_t = 0
				#print repr(data_dict)
				for d in data_dict["data"]:
					_t = float(d[0])/1000.0 # spkjson returns times in javascript time, milliseconds since epoch, convert to unix time of seconds since epoch
					# this seems to work to have the data in the right time
					# will need to keep an eye on it though
					# The Unix timestamp is in local time (timezone passed in URL)
					_dt = datetime.datetime.fromtimestamp(_t,pytz.utc)
					#_dt = _dt.astimezone(self.tz)
					_dt = _dt.replace(tzinfo=self.tz)
					#print("_dt: %s" % repr(_dt))
					#print _dt
					if d[1] is not None:
						#print("Reading value: %s" % d[1])
						_v = float(d[1]) # does not currently implement text operations
					else:
						_v = None
					_q = int(d[2])
					self.values.append( ( _dt,_v,_q  ) )

				if self.time:
					self.type = "SCALAR"
					self.value = self.values[0][1]
			except Exception as err:
				print( repr(err) + " : " + str(err) )
		elif self.dbtype.upper() == "JSON":
			import json, http.client as httplib, urllib.parse as urllib

			#fmt = "%d-%b-%Y %H%M"
			fmt = "%Y-%m-%dT%H:%M:%S"
			tz = self.tz
			units = self.dbunits
			ts_name = ".".join( (self.dbloc, self.dbpar, self.dbptyp, str(self.dbint), str(self.dbdur), self.dbver) )

			# Loop until we fetch some data, if missing is NOMISS
			retry_count = 10			# Go back at most this many weeks + 1
			sstart = self.start
			send = self.end

			while(retry_count > 0):
				# Convert time to destination timezone
				# Should this actually convert the time to the destination time zone (astimezone), or simply swap the TZ (replace)?
				# 'astimezone' would be the "proper" behavior, but 'replace' mimics repgen_4.
				start = tz.localize(sstart.replace(tzinfo=None))
				end = tz.localize(send.replace(tzinfo=None))
				query = self.query
				
				if query is None:
					query = ""

				sys.stderr.write("Getting %s from %s to %s in tz %s, with units %s\n" % (ts_name,start.strftime(fmt),end.strftime(fmt),str(tz),units))
				query = f"/{query}/timeseries?"
				params = urllib.urlencode( {
					"name": ts_name,
					"unit": units,
					"begin": start.strftime(fmt),
					"end":   end.strftime(fmt),
					"timezone": str(tz),
					"pageSize": -1,					# always fetch all results
				})
				try:
					print("Fetching: %s" % self.host+query+params)
					conn = None
					headers = { 'Accept': "application/json;version=2" }

					if sys.platform != "win32" and self.timeout:
						# The SSL handshake can sometimes fail and hang indefinitely
						# inflate the timeout slightly, so the socket has a chance to return a timeout error
						# This is a failsafe to prevent a hung process
						signal.alarm(int(self.timeout * 1.1) + 1)

					try:
						from repgen.util.urllib2_tls import TLS1Connection
						conn = TLS1Connection( self.host, timeout=self.timeout )
						conn.request("GET", query+params, None, headers )
					except SSLError as err:
						print(type(err).__name__ + " : " + str(err))
						print("Falling back to non-SSL")
						# SSL not supported (could be standalone instance)
						conn = httplib.HTTPConnection( self.host, timeout=self.timeout )
						conn.request("GET", query+params, None, headers )

					if sys.platform != "win32" and self.timeout:
						signal.alarm(0) # disable the alarm

					r1 = conn.getresponse()
					data = r1.read()
					
					if r1.status != 200:
						print("HTTP Error " + str(r1.status) + ": " + str(data))
						return

					data_dict = None

					try:
						data_dict = json.loads(data)
					except json.JSONDecodeError as err:
						print(str(err))
						print(repr(data))

					# get the depth
					prev_t = 0
					#print repr(data_dict)

					if len(data_dict["values"]) > 0:
						for d in data_dict["values"]:
							_t = float(d[0])/1000.0 # json returns times in javascript time, milliseconds since epoch, convert to unix time of seconds since epoch
							_dt = datetime.datetime.fromtimestamp(_t,pytz.utc)
							_dt = _dt.astimezone(self.tz)
							#_dt = _dt.replace(tzinfo=self.tz)
							#print("_dt: %s" % repr(_dt))
							#print _dt
							if d[1] is not None:
								#print("Reading value: %s" % d[1])
								_v = float(d[1]) # does not currently implement text operations
							else:
								_v = None
							_q = int(d[2])
							self.values.append( ( _dt,_v,_q  ) )

					if self.ismissing():
						if self.missing == "NOMISS":
							sstart = sstart - timedelta(weeks=1)
							retry_count = retry_count - 1
							continue

					if self.time:
						self.type = "SCALAR"
						if self.missing == "NOMISS":
							# Get the last one, in case we fetched extra because of NOMISS
							for v in reversed(self.values):
								if v is not None and v[1] is not None:
									self.value = v[1]
									break
						elif len(self.values) > 0:
							self.value = self.values[-1][1]

				except Exception as err:
					print( repr(err) + " : " + str(err) )

				break

		elif self.dbtype.upper() == "DSS":
			raise Exception("DSS retrieval is not currently implemented")

	# math functions
	def __add__( self, other ):
		return self.domath(operator.add,other)

	def __sub__( self, other ):
		return self.domath( operator.sub, other )

	def __rsub__( self, other ):
		return self.domath( extra_operator.rsub, other )

	def __mul__( self, other ):
		return self.domath( operator.mul, other)

	def __rmul__( self, other ):
		return self.domath( operator.mul, other)

	def __truediv__(self,other):
		return self.domath( operator.truediv,other)


	def domath(self,op,other):
		typ = Value.shared["dbtype"]
		tmp = Value(dbtype="copy")
		tmp.picture=self.picture
		Value.shared["dbtype"]=typ
		print( "Doing Op %s on %s with other %s" % (repr(op),repr(self),repr(other) ) )
		if isinstance( other, number_types ) and self.type=="TIMESERIES":
			for v in self.values:
				if (v is not None) and (v[1] is not None) and (other is not None):
					if isinstance(v[1], Decimal) and not isinstance(other, Decimal):
						tmp.values.append( (v[0],op(v[1], Decimal.from_float(other)),v[2]) )
					else:
						tmp.values.append( (v[0],op(v[1], other),v[2]) )
				else:
					tmp.values.append( ( v[0], None, v[2] ) )
		elif isinstance( other, (*number_types,datetime.timedelta,timedelta) ) and self.type=="SCALAR":
			if (self.value is not None) and (other is not None):
				if self.ismissing() or self.ismissing(other):
					tmp.value = self.missdta
				else:
					if isinstance(self.value, Decimal) and not isinstance(other, Decimal):
						tmp.value = op(self.value,Decimal.from_float(other))
					else:
						tmp.value = op(self.value,other)
			else:
				tmp.value = None
			tmp.type="SCALAR"
		elif isinstance( other, (*number_types,datetime.timedelta,timedelta) ) and self.type=="TIMESERIES" and len(self.values) == 1:
			if (self.values[0] is not None) and (other is not None):
				if self.ismissing() or self.ismissing(other):
					tmp.value = self.missdta
				else:
					tmp.value = op(self.values[0][1],other)
			else:
				tmp.value = None
			tmp.type="SCALAR"
		elif isinstance( other, Value ):
			if self.type == "SCALAR" and other.type == "SCALAR":
				if self.known() and other.known():
					tmp.value = op(self.value,other.value)
				else:
					if self.value is None or other.value is None:
						tmp.value = None
					else:
						if self.missing == "MISSOK":
							tmp.value = self.missdta
						else:
							tmp.value = None
				tmp.type = "SCALAR"
			elif self.type =="TIMESERIES" and other.type == "SCALAR":
				old_trap = getcontext().traps[DivisionByZero]
				getcontext().traps[DivisionByZero] = False
				for v in self.values:
					if (v[1] is not None) and (other.value is not None):
						if self.ismissing(v[1]) or self.ismissing(other):
							tmp.values.append( (v[0], self.missdta, 5) )
						else:
							try:
								tmp.values.append( (v[0], op(v[1],other.value), v[2] ) )
							except (DivisionByZero,ZeroDivisionError):
								tmp.values.append( (v[0], float('NaN'), v[2] ) )
					else:
						tmp.values.append( (v[0], None, v[2] ) )
				getcontext().traps[DivisionByZero] = old_trap
			elif self.type =="SCALAR" and other.type == "TIMESERIES":
				old_trap = getcontext().traps[DivisionByZero]
				getcontext().traps[DivisionByZero] = False
				for v in other.values:
					if (v[1] is not None) and (self.value is not None):
						try:
							tmp.values.append( (v[0], op(v[1],self.value), v[2] ) )
						except (DivisionByZero,ZeroDivisionError):
							tmp.values.append( (v[0], float('NaN'), v[2] ) )
					else:
						if self.ismissing() or self.ismissing(v[1]):
							tmp.values.append( (v[0], self.missdta, 5) )
						else:
							tmp.values.append( (v[0], None, v[2] ) )
				getcontext().traps[DivisionByZero] = old_trap

			elif self.type=="TIMESERIES" and other.type == "TIMESERIES":
			# loop through both arrays
				# for now just implement intersection
				for v_left in self.values:
					for v_right in other.values:
						if v_left[0] == v_right[0]: # times match
							if (v_left[1] is not None) and (v_right[1] is not None):
								tmp.values.append( (v_left[0],op( v_left[1], v_right[1] ), v_left[2] ) )
							else:
								if self.ismissing(v_left[0]) or self.ismissing(v_right[1]):
									tmp.values.append( (v_left[0], self.missdta, 5) )
								else:
									tmp.values.append( (v_left[0], None, v_left[2] ) )
			else:
				return NotImplemented
		else:
			return NotImplemented
		return tmp

	# Only implement comparisons against SCALAR quantities
	# equality will also compare strings
	def __eq__(self, other):
		if isinstance(other, Value):
			if self.type == "SCALAR" and other.type == "SCALAR":
				return self.value == other.value
		elif isinstance(other, (*number_types, str)) and self.type == "SCALAR":
			return self.value == other
		return NotImplemented

	def __gt__(self, other):
		if isinstance(self.value, str):
			return False
		if isinstance(other, Value):
			if self.type == "SCALAR" and other.type == "SCALAR":
				return other.value is not None and self.value is not None and self.value > other.value
			elif self.type == "TIMESERIES" and len(self.values) <= 1 and other.type == "SCALAR":
				return other.value is not None and len(self.values) > 1 and self.values[0] is not None and self.values[0] > other.value
			elif self.type == "SCALAR" and other.type == "TIMESERIES" and len(other.values) <= 1:
				return len(other.values) > 0 and self.value > other.value[1]
			elif self.type == "TIMESERIES" and len(self.values) <= 1 and other.type == "TIMESERIES" and len(other.values) <= 1:
				return len(other.values) > 0 and len(self.values) > 1 and self.values[0] is not None and other.values[0] is not None and self.values[0] > other.value[0]

		elif isinstance(other, number_types) and self.type == "SCALAR":
			return self.value is not None and self.value > other
		elif isinstance(other, number_types) and self.type == "TIMESERIES" and len(self.values) <= 1:
			return self.value is not None and len(self.values) > 0 and self.values[0] is not None and self.values[0] > other
		return NotImplemented

	def __ge__(self, other):
		if isinstance(self.value, str):
			return False
		if isinstance(other, Value):
			if self.type == "SCALAR" and other.type == "SCALAR":
				return other.value is not None and self.value is not None and self.value >= other.value
		elif isinstance(other, number_types) and self.type == "SCALAR":
			return self.value is not None and self.value >= other
		elif isinstance(other, number_types) and self.type == "TIMESERIES" and len(self.values) <= 1:
			return len(self.values) > 0 and self.values[0] is not None and (self.values[0] - other) > 0.01
		return NotImplemented

	def __lt__(self, other):
		if isinstance(self.value, str):
			return False
		if isinstance(other, Value):
			if self.type == "SCALAR" and other.type == "SCALAR":
				return other.value is not None and self.value is not None and self.value < other.value
			elif self.type == "TIMESERIES" and len(self.values) <= 1 and other.type == "SCALAR":
				return other.value is not None and len(self.values) > 1 and self.values[0] is not None and self.values[0] < other.value
			elif self.type == "SCALAR" and other.type == "TIMESERIES" and len(other.values) <= 1:
				return len(other.values) > 0 and self.value < other.value[1]
			elif self.type == "TIMESERIES" and len(self.values) <= 1 and other.type == "TIMESERIES" and len(other.values) <= 1:
				return len(other.values) > 0 and len(self.values) > 1 and self.values[0] is not None and other.values[0] is not None and self.values[0] < other.value[0]

		elif isinstance(other, number_types) and self.type == "SCALAR":
			return self.value is not None and self.value < other
		return NotImplemented

	def __le__(self, other):
		if isinstance(self.value, str):
			return False
		if isinstance(other, Value):
			if self.type == "SCALAR" and other.type == "SCALAR":
				return other.value is not None and self.value is not None and (self.value - other.value) < 0.01
		elif isinstance(other, number_types) and self.type == "SCALAR":
			return self.value is not None and (self.value - other) < 0.01
		elif isinstance(other, number_types) and self.type == "TIMESERIES" and len(self.values) <= 1:
			return len(self.values) > 0 and self.values[0] is not None and (self.values[0] - other) < 0.01
		return NotImplemented

	def __str__(self):
		if self.type=="SCALAR":
			return self.format(self.value)
		else:
			return "Unable to process at this time"
	def __repr__(self):
		return "<Value,type=%s,value=%s,len values=%d, picture=%s>" % (self.type,str(self.value),len(self.values),self.picture)

	def format(self,value):
		#print repr(value)
		if self.ismissing(value) or isinstance(value, list):
			return self.misstr

		if isinstance(value, number_types):
			# The picture might have prefix or suffix text, e.g, "   %4.1f V"
			# Split that out
			specifier_start = self.picture.find('%')
			if specifier_start == -1: return self.picture					# If there's no '%', just return the picture itself

			prefix = self.picture[0:specifier_start]
			picture = re.search(r"%([0-9.,+-]+[bcdoxXneEfFgG%])", self.picture).group(1)
			suffix = self.picture[self.picture.index(picture) + len(picture):]

			if isinstance(value, (Decimal,float)) and not math.isfinite(value):
				result = prefix + self.undef + suffix
			else:
				if self.ismissing(value):
					result = prefix + self.misstr + suffix
				elif value is None:
					result = prefix + self.undef + suffix
				else:
					result = prefix + format(value + 0, picture) + suffix	# Add 0 to correct any negative 0 values
			if self.ucformat: result = result.upper()
			return result
		elif isinstance(value, datetime.datetime) :
			result = None
			if "%K" in self.picture:
				tmp = self.picture.replace("%K","%H")
				tmpdt = value.replace(hour=value.hour)
				if not tmpdt.tzinfo:
					tmpdt = self.tz.localize(tmpdt)
				tmpdt = tmpdt.astimezone(self.tz)	# Make sure datetime is in the requested timezone for display
				if tmpdt.hour == 0 and tmpdt.minute==0:
					tmp = tmp.replace("%H","24")
					tmpdt = tmpdt - timedelta(days=1) # get into the previous date
				result = tmpdt.strftime(tmp)
			elif value.hour == 0 and value.minute == 0 and value.second == 0 and not "%H" in self.picture:
				# If the time is exactly midnight, but not printing the time,
				# subtract a day for displaying the correct date
				tmpdt = value - timedelta(days=1) # get into the previous date
				if not tmpdt.tzinfo:
					tmpdt = self.tz.localize(tmpdt)
				tmpdt = tmpdt.astimezone(self.tz)	# Make sure datetime is in the requested timezone for display
				result = tmpdt.strftime(self.picture)
			else:
				result = value.strftime(self.picture)
			# If compat option was set, upper case the dates in the report
			if self.ucformat: result = result.upper()
			return result
		elif isinstance(value, str):
			return value
		else:
			return self.undef

	# will need implementations of add, radd, mult, div, etc for use in math operations.
	def pop(self):
		if self.type == "SCALAR":
			return self.format(self.value)
		elif self.type == "TIMESERIES":
			if self.index is None:
				self.index = 0
			self.index = self.index+1
			try:
				#print repr(self.values[self.index-1])
				return self.format(self.values[self.index-1][1])
			except IndexError:
				# If data is missing, just return the undefined string value
				return self.undef
			except Exception as err:
				print(repr(err) + " : " + str(err), file=sys.stderr)
				return self.undef

	def datatimes(self):
		"""
			Returns a new Value where the values are replaced by the datetimes
		"""
		typ = Value.shared["dbtype"]
		tmp = Value(dbtype="copy")
		Value.shared["dbtype"]=typ

		if self.type == "TIMESERIES":
			for v in self.values:
				tmp.values.append( (v[0],v[0],v[2]) )
		elif self.type == "SCALAR":
			tmp.time = self.time
			tmp.values.append( (self.time, self.time, None) )
		return tmp

	def qualities(self):
		"""
			Returns a new Value where the values are replace by the qualities
		"""
		typ = Value.shared["dbtype"]
		tmp = Value(dbtype="copy")
		Value.shared["dbtype"]=typ
		for v in self.values:
			tmp.values.append( (v[0],v[2],v[2]) )
		return tmp

	def set_time( self, **kwarg  ):
		if self.type == "SCALAR" and isinstance( self.value, datetime.datetime ):
			self.value = self.value.replace( **kwarg )
		else:
			raise Exception("Not implemented for the requested change")

	# This implements the ELEMENT() repgen function
	def element(self, nearest, dtarg, missval):
		"""
			Returns the element at the specified datetime. Searches forward or backard if BEFORE or AFTER is passed.
		"""
		if self.type =="TIMESERIES":
			typ = Value.shared["dbtype"]
			tmp = Value(dbtype="copy")
			tmp.value = None
			tmp.type = "SCALAR"

			dt = dtarg
			if isinstance(dtarg,Value):
				dt = dtarg.value
				if not dt.tzinfo: dt = dtarg.tz.localize(dt)

			# If the passed in argument doesn't have a time zone, add one
			if not dt.tzinfo:
				dt = self.tz.localize(dt)
			try:
				tmp.value = self[dt][1]
			except:
				if missval == "MISSOK":
					tmp.value = tmp.misstr
				elif missval == "NOMISS":
					if nearest == "AT":
						return tmp	# Undefined
					else:
						if nearest == "BEFORE":
							for v in reversed(self.values):
								if v[0] <= dt and v[1] is not None:
									tmp.value = v[1]
									tmp.time = v[0]
									break
						elif nearest == "AFTER":
							for v in self.values:
								if v[0] >= dt and v[1] is not None:
									tmp.value = v[1]
									tmp.time = v[0]
									break
			return tmp
		else:
			raise Exception("operation only valid on a time series")

	def last(self):
		if self.type =="TIMESERIES":
			typ = Value.shared["dbtype"]
			tmp = Value(dbtype="copy")
			Value.shared["dbtype"]=typ
			tmp.value = None
			tmp.type ="SCALAR"
			try:
				tmp.value = self.values[ len(self.values)-1 ] [1]
				tmp.time = self.values[ len(self.values)-1 ] [0]
			except Exception as err:
				print("Issue with getting last value -> %s : %s" % (repr(err),str(err)), file=sys.stderr)
			return tmp
		else:
			return None

	def __getitem__( self, arg ):
			dt = arg
			is_slice = False
			start = None
			end = None

			if isinstance(arg,slice):
				is_slice = True
				start = arg.start
				end = arg.stop
				dt = start

			if isinstance(dt,Value):
				dt = self.tz.localize(dt.value)
			if not is_slice:
				start = end = dt

			if isinstance(start,Value):
				start = start.value
				start = start.replace(tzinfo=None)
				start = self.tz.localize(start)
			if isinstance(end,Value):
				end = end.value
				end = end.replace(tzinfo=None)
				end = self.tz.localize(end)

			# If the passed in argument doesn't have a time zone, add one
			if isinstance(dt, datetime.datetime) and not dt.tzinfo:
				dt = self.tz.localize(dt)

			if self.type == "TIMESERIES":
				typ = Value.shared["dbtype"]
				tmp = Value(dbtype="copy")
				Value.shared["dbtype"]=typ
				tmp.value = None
				if is_slice: tmp.type = "TIMESERIES"
				else: tmp.type = "SCALAR"
				haveval = False
				tmp.missing = self.missing

				if isinstance(dt,int):
					try:
						if is_slice:
							for x in range(start, end):
								try:
									tmp.values.append(self.values[x])
									haveval = True
								except IndexError:
									# Ignore missing values in the range
									pass
						else:
							tmp.value = self.values[dt][1]
							tmp.time = self.values[dt][0]
							haveval = True
					except IndexError:
						pass
				else:
					for v in self.values:
						if is_slice:
							if v[0] >= start and v[0] <= end:
								tmp.values.append(v)
								haveval = True
						else:
							if v[0] == dt:
								tmp.value = v[1]
								tmp.time = v[0]
								haveval = True
								break

				if haveval == True:
					return tmp
				else:
					if self.missing == "EXACT":
						return tmp
					elif self.missing == "MISSOK":
						return tmp
			else:
				raise Exception("date index only valid on a timeseries")

	"""
		The following are static methods as they can be applied to multiple time series/values at a time

		all functions should process a keyword treat which determines how the function will respond
		to missing values

		valid values for treat will be the following:

		a number   - if a value is missing (None) use this number in its place
		a tuple of numbers - if a value is missing, substitute in order of the arguments these replacement values
		"IGNORE"   - operate as if that row/value wasn't there
		"MISS"     - if any given value is missing, the result is missing (This is the default)
	
		Generally args should be either a list of SCALAR values (actual number types are okay)
		or a single time series.
		
		
	"""
				

	@staticmethod
	def apply( function, *args, **kwargs ):
		"""
			apply an arbitrary user function to the provided data.
			the inputs to the function must the same number of and in the same order as passed into args
			the function is called using function(*args)
			
			the function must return a number or None
			the function can be any callable object: function,lambda, class with a __call__ method, etc
			
		"""
		returns = 1 #
		try:
			returns = int(kwargs["returns"])
		except:
			pass
		values = []
		typ = Value.shared["dbtype"]
		for i in range(0,returns):
			tmp = Value(dbtype="copy")
			tmp.values = []
			tmp.value = None
			values.append( tmp )
			Value.shared["dbtype"]=typ
		
		times = Value.gettimes(*args,**kwargs)
		if len(times)==0:
			tmp.type = "SCALAR"
			# get the numbers of everything
			#tmp.value = function( *args )
			ret = function( *args )
			if isinstance( ret, (list,tuple) ):
				for i in range(len(ret)):
					values[i].value = ret[i]
					
			else:
				values[0].value = ret
		elif len(times) > 0:
			for t in times:
				vars = []
				for arg in args:
					if isinstance( arg, (int,float,complex) ):
						vars.append(arg) 
					elif isinstance( arg, Value ) and arg.type=="SCALAR":
						vars.append( arg.value) # need to handle missing value (.value is None)						
					elif isinstance( arg, Value ) and arg.type=="TIMESERIES":
						try:
							v = arg[t].value
							vars.append(v)
						except KeyError as err:
							vars.append(None) # here we handle the missing value logic
				res = None
				try:
					res = function( *vars )
				except Exception as err:
					print("Failed to compute a values %s : %s" % (repr(err),repr(str)), file=sys.stderr)
				if isinstance( res, (list,tuple)):
					for i in range(len(res)):
						values[i].values.append( (t,res[i],0) )
				else:
					values[0].values.append( ( t,res,0) )

		return values

	@staticmethod
	def min( *args, **kwarg ):
		"""
			this is an exception to the one timeseries rule.
			Find the minimum of all passed in values
		"""
		tmp = Value.mktmp()
		tmp.value = None
		tmp.type="SCALAR"
		treat=Value.gettreat(**kwarg)

		for arg in args:
			if isinstance( arg, number_types ):
				if tmp.value is None or tmp.value > arg:
					tmp.value = arg
			elif arg.type =="SCALAR":
				if arg.value is not None and (tmp.value is None or tmp.value > arg.value):
					tmp.value = arg.value
				else:
					if isinstance( treat, number_types):
						if tmp.value is None or tmp.value > arg:
							tmp.value = treat
					elif treat=="MISS":
						tmp.value = None
						return tmp
					# else, move to the next value
			elif arg.type == "TIMESERIES":
				for row in arg.values:
					v = row[1]
					if tmp.value is None or (v is not None and tmp.value > v):
						tmp.value = v
					else:
						if isinstance( treat, number_types):
							tmp.value = treat
						elif treat=="MISS":
							tmp.value = None
							return tmp

		return tmp

	@staticmethod
	def max( *args, **kwarg ):
		"""
			this is an exception to the one timeseries rule.
			Find the minimum of all passed in values
		"""
		tmp = Value.mktmp()
		tmp.value = None
		tmp.type="SCALAR"
		treat=Value.gettreat(**kwarg)

		for arg in args:
			if isinstance( arg, number_types ):
				if tmp.value is None or tmp.value < arg:
					tmp.value = arg
			elif arg.type =="SCALAR":
				if arg.value is not None:
					if tmp.value is None or tmp.value < arg.value:
						tmp.value = arg.value
				else:
					if isinstance( treat, number_types):
						if tmp.value is None or tmp.value < arg:
							tmp.value = treat
					elif treat=="MISS":
						tmp.value = None
						return tmp
					# else, move to the next value
			elif arg.type == "TIMESERIES":
				for row in arg.values:
					v = row[1]
					if tmp.value is None or (v is not None and tmp.value < v):
						tmp.value = v
					else:
						if isinstance( treat, number_types):
							tmp.value = treat
						elif treat=="MISS":
							tmp.value = None
							return tmp

		return tmp

	@staticmethod
	def sum( *args, **kwarg ):
		"""
			this is an exception to the one timeseries rule.
			we assume the user wants whatever values passed summed up into
			one value
		"""
		tmp = Value.mktmp()
		tmp.value = 0
		tmp.type="SCALAR"
		treat=Value.gettreat(**kwarg)
		
		for arg in args:
			if isinstance( arg, number_types ):
				tmp.value += arg
			if isinstance( arg, Value ):
				if arg.type =="SCALAR":
					if arg.value is not None:
						if arg.ismissing() and treat == "MISS":
							tmp.value = Value.shared["missdta"]
							break
						tmp.value += arg.value
					else:
						# Undefined values are ignored
						if len(arg.values) == 1 and arg.values[0][1] is not None:
							if arg.ismissing(arg.values[0][1]) and treat == "MISS":
								tmp.value = Value.shared["missdta"]
								break
							tmp.value += arg.values[0][1]
						elif isinstance( treat, number_types):
							tmp.value += treat
					# else, move to the next value
				elif arg.type == "TIMESERIES":
					for row in arg.values:
						v = row[1]
						if v is not None:
							if arg.ismissing(v) and treat == "MISS":
								tmp.value = Value.shared["missdta"]
								break
							tmp.value += v
						else:
							if isinstance( treat, number_types):
								tmp.value += treat
				elif arg.type == "GROUP":
					tmp.value += Value.sum(*arg.value).value

		return tmp

	@staticmethod
	def average( *args, **kwarg ):
		tmp = Value.mktmp()
		tmp.value = 0
		tmp.type="SCALAR"
		treat = Value.gettreat(**kwarg)
		numvals = 0
		if len(args) > 1:
			for arg in args:
				if arg.type=="TIMESERIES":
					raise Exception("Time series not allowed with multiple values")

		if len(args) == 1:
			if args[0].type == "SCALAR":
				tmp.value = args[0].value
			elif args[0].type == "GROUP":
				for val in args[0].value:
					if val.type == "SCALAR":
						if val.known():
							tmp.value += val.value
							numvals += 1
						elif treat == "MISS":
							tmp.value = args[0].missdta
							return tmp
						elif treat == "ZERO":
							numvals += 1
					else:
						raise Exception("GROUPed data can only be used with SCALARs.")
				if numvals == 0 and args[0].missing == "MISSOK":
					tmp.value = args[0].missdta
			else: # time series
				for row in args[0].values:
					v = row[1]
					if args[0].known(v):
						tmp.value += v
						numvals += 1
					elif treat == "MISS":
						tmp.value = args[0].missdta
						return tmp
					elif treat == "ZERO":
						numvals += 1
				# What is the defined behavior in repgen4?
				if numvals == 0 and args[0].missing == "MISSOK":
					tmp.value = args[0].missdta

		else:
			for arg in args:
				if isinstance( arg, number_types ):
					tmp.value += arg
					numvals += 1
				else:
					if arg.known():
						tmp.value += arg.value
						numvals += 1
					elif treat=="MISS":
						tmp.value = args[0].missdta
						return tmp
					elif treat == "ZERO":
						numvals += 1

		if numvals == 0:
			return tmp

		if isinstance(tmp.value, Decimal):
			tmp.value = tmp.value/Decimal.from_float(float(numvals))
		else:
			tmp.value = tmp.value/float(numvals)
		return tmp
		

	@staticmethod
	def count(*args ):
		"""
			This function ignores the only 1 timeseries rule and just counts the number of non-missing
			values in all the variables passed in.
			It also doesn't take any keywords
		"""
		tmp = Value.mktmp()
		tmp.value = 0
		tmp.type = "SCALAR"
		for arg in args:
			if isinstance(arg, number_types):
				tmp.value+=1
			elif isinstance(arg, Value) and arg.type =="SCALAR" and arg.value is not None:
				tmp.value+=1
			elif isinstance(arg, Value) and arg.type =="TIMESERIES":
				for row in arg.values:
					if row[1] is not None:
						tmp.value+=1
		
		return tmp

	@staticmethod
	def accum(arg,**kwarg ):
		"""
			This function requires a single time series and nothing else

			treat
			number = use the number
			ignore = current value is missing, but otherwise keep accumulating
			miss = stop accumulating after the first missing input
		"""
		tmp = Value.mktmp()
		tmp.type="TIMESERIES"
		tmp.values = []
		treat = Value.gettreat(**kwarg)
		accum = 0
		previous = 0
		for row in arg.values:
			dt,v,q = row
			cur = None
			
			if v is not None and not ((previous is None) and (treat=="MISS")) :
				accum += v
				cur = accum
			elif v is None and ((previous is None) and (treat=="MISS")):
				cur = None
			elif isinstance(treat, number_types):
				accum += v
			else:
				cur = None

			previous=v


			tmp.values.append( (dt,cur,q) )
		return tmp

	@staticmethod
	def diff(arg,**kwarg ):
		"""
			This function requires a single time series and nothing else

			treat
			number = use the number
			ignore = current value is missing, but otherwise keep accumulating
			miss = stop accumulating after the first missing input

		"""
		tmp = Value.mktmp()
		tmp.type="TIMESERIES"
		tmp.values = []
		treat = Value.gettreat(**kwarg)
		accum = 0
		previous = None
		treat_val = 0

		if isinstance(treat, number_types):
			treat_val = float(treat)

		for row in arg.values:
			dt,v,q = row
			qual = q
			cur = None

			if v is not None and previous is not None:
				cur = v - previous
			elif v is None and previous is not None:
				if treat == "ZERO":
					cur = treat_val - previous
				elif treat == "MISS":
					cur = None
					qual = 5
			elif v is not None and previous is not None:
				if treat == "ZERO":
					cur = v - treat_val
				elif treat == "MISS":
					cur = None
					qual = 5
			else:
				if treat == "ZERO":
					cur = 0
				elif treat == "MISS":
					cur = None
					qual = 5

			previous = v
			tmp.values.append( (dt,cur,qual) )
		return tmp

	def roundpos(self, place):
		"""
			Rounds the variable to the specified 'place', where place is a power of 10 exponent.
			For example, -1 means round to the nearest tenths place (0.1).
		"""
		tmp = Value.mktmp()
		tmp.value = 0
		tmp.type = self.type
		place_decimal = 0
		place_float = 0

		if isinstance(place, Value):
			if place.type != "SCALAR": raise ValueError("Cannot specify a timeseries as the decimal rounding place.")
			place = place.value

		if not isinstance(place, number_types): raise ValueError("Decimal rounding place must be a number.")
		place_decimal = Decimal(10) ** place		# Convert integer places to floating value supported by quantize (-1 -> 0.1)
		place_float = -place						# Python decimal place is inverted from repgen4
		
		if self.type == "SCALAR" and self.value is not None:
			if isinstance(self.value, Decimal):
				tmp.value = Decimal(self.value).quantize(place_decimal)
			else:
				tmp.value = round(float(self.value), place_float)
		elif self.type =="TIMESERIES":
			for d,v,q in self.values:
				if v is not None:
					if isinstance(v, Decimal):
						v = Decimal(v).quantize(place_decimal)
					else:
						v = round(float(v), place_float)
				tmp.values.append([d,v,q])
		
		return tmp

	# Returns true if SCALAR and value is defined, or TIMESERIES and there's at least one value
	# if value is set to the classname, use the instance's value
	def known(self, value=__qualname__):
		if isinstance(value, str) and value == Value.__name__:
			if self.type == "SCALAR":
				return self.value is not None and not self.ismissing()
			elif self.type == "TIMESERIES":
				if self.values is not None:
					for v in self.values:
						if v[1] is not None and not self.ismissing(v[1]):
							return True
			# TODO: GROUP
		else:
			return value is not None and not self.ismissing(value)
		return False

	# assumes data is not None (undefined)
	# undefined data is not missing
	# if value is set to the classname, use the instance's value
	def ismissing(self, value=__qualname__):
		if isinstance(value, str) and value == Value.__name__:
			if self.type == "SCALAR" and not self.ismissing(self.value):
				return False
			elif self.type == "TIMESERIES":
				for val in self.values:
					if not self.ismissing(val[1]):
						return False
			# TODO: GROUP
			return True
		else:
			if isinstance(self.missdta, list):
				return value in self.missdta
			else:
				return value == self.missdta

	@staticmethod
	def gettimes( *args,**kwargs ):
		# build a new last that has the intersection or union (user specified, just implement intersection for now
		# scalar values will just get copied in time, we do need to maintain the order of the input args.
		timesets = []
		times = []
		for arg in args:
			if isinstance( arg, Value) and arg.type == "TIMESERIES":
				timesets.append( set( [x[0] for x in arg.values ]) )
		if len(timesets) > 0:
			if len(timesets)==1:
				times = list(timesets[0])
			else:
				times =list( timesets[0].intersection( *timesets[1:] ) ) # here we should check for intersection or union

		times.sort() # make sure everything is in time ascending order
		return times

	@staticmethod
	def mktmp():
		typ = Value.shared["dbtype"]
		tmp = Value(dbtype="copy")
		Value.shared["dbtype"]=typ
		return tmp
	@staticmethod
	def gettreat(**kwarg):
		treat = "MISS"
		for key in kwarg:
			if key.lower() == "treat":
				treat= kwarg[key]
		if isinstance(treat, string_types):
			treat=treat.upper()
		return treat
