from os import system, listdir, statvfs, popen, makedirs, stat, major, minor, path, access, readlink, symlink, rmdir, unlink, rename
from Tools.Directories import SCOPE_HDD, resolveFilename
from Tools.BoundFunction import boundFunction
from Tools.CList import CList
from SystemInfo import SystemInfo
import time
import re, shutil
from Components.Console import Console
from config import config, configfile, ConfigYesNo, ConfigText, ConfigSubDict, ConfigSubsection, ConfigBoolean

def MajorMinor(path):
	rdev = stat(path).st_rdev
	return (major(rdev),minor(rdev))

def readFile(filename):
	file = open(filename)
	data = file.read().strip()
	file.close()
	return data

DEVTYPE_UDEV = 0
DEVTYPE_DEVFS = 1

class Harddisk:
	def __init__(self, device, removable = False):
		self.device = device
		self.isRemovable = removable

		if access("/dev/.udev", 0):
			self.type = DEVTYPE_UDEV
		elif access("/dev/.devfsd", 0):
			self.type = DEVTYPE_DEVFS
		else:
			print "Unable to determine structure of /dev"

		self.max_idle_time = 0
		self.idle_running = False
		self.timer = None

		self.dev_path = ''
		self.disk_path = ''
		self.phys_path = path.realpath(self.sysfsPath('device'))

		if self.type == DEVTYPE_UDEV:
			self.dev_path = '/dev/' + self.device
			self.disk_path = self.dev_path

		elif self.type == DEVTYPE_DEVFS:
			tmp = readFile(self.sysfsPath('dev')).split(':')
			s_major = int(tmp[0])
			s_minor = int(tmp[1])
			for disc in listdir("/dev/discs"):
				dev_path = path.realpath('/dev/discs/' + disc)
				disk_path = dev_path + '/disc'
				try:
					rdev = stat(disk_path).st_rdev
				except OSError:
					continue
				if s_major == major(rdev) and s_minor == minor(rdev):
					self.dev_path = dev_path
					self.disk_path = disk_path
					break

		print "new Harddisk", self.device, '->', self.dev_path, '->', self.disk_path
		if not self.isRemovable:
			self.startIdle()

	def __lt__(self, ob):
		return self.device < ob.device

	def partitionPath(self, n):
		if self.type == DEVTYPE_UDEV:
			return self.dev_path + n
		elif self.type == DEVTYPE_DEVFS:
			return self.dev_path + '/part' + n

	def sysfsPath(self, filename):
		return path.realpath('/sys/block/' + self.device + '/' + filename)

	def stop(self):
		if self.timer:
			self.timer.stop()
			self.timer.callback.remove(self.runIdle)

	def bus(self):
		# CF (7025 specific)
		if self.type == DEVTYPE_UDEV:
			ide_cf = False	# FIXME
		elif self.type == DEVTYPE_DEVFS:
			ide_cf = self.device[:2] == "hd" and "host0" not in self.dev_path

		internal = "pci" in self.phys_path

		if ide_cf:
			ret = "External (CF)"
		elif internal:
			ret = "Internal"
		else:
			ret = "External"
		return ret

	def bus_type(self):
		# CF (7025 specific)
		if self.type == DEVTYPE_UDEV:
			ide_cf = False	# FIXME
		elif self.type == DEVTYPE_DEVFS:
			ide_cf = self.device[:2] == "hd" and "host0" not in self.dev_path
			
		sata = "pci" in self.phys_path
		sata_desc = self.bus_description()

		if ide_cf:
			ret = "IDE"
		elif sata:
			if sata_desc is not None:
				ret = sata_desc
			else:
				ret = "SATA"
		else:
			ret = "USB"
		return ret

	def bus_description(self):
		phys = self.phys_path[4:]
		from Tools.HardwareInfo import HardwareInfo
		if self.device.find('sr') == 0 and self.device[2].isdigit():
			devicedb = DEVICEDB_SR
		else:
			devicedb = DEVICEDB

		for physdevprefix, pdescription in devicedb.get(HardwareInfo().device_name,{}).items():
			if phys.startswith(physdevprefix):
				return pdescription
		if phys.find('pci') != -1:
			return "SATA"
		elif phys.find('usb') != -1:
			return "USB"
		return "External Storage"

	def diskSize(self):
		try:
			line = readFile(self.sysfsPath('size'))
			cap = int(line)
		except:
			return 0;
		return cap / 1000 * 512 / 1000

	def capacity(self):
		cap = self.diskSize()
		if cap == 0:
			return ""
		return "%d.%03d GB" % (cap/1000, cap%1000)

	def model(self, model_only = False, vendor_only = False):
		if self.device[:2] == "hd":
			return readFile('/proc/ide/' + self.device + '/model')
		elif self.device[:2] == "sd":
			try:
				vendor = readFile(self.sysfsPath('device/vendor'))
				model = readFile(self.sysfsPath('device/model'))
			except:
				vendor = ""
				model = ""
				
			if vendor_only:
				return vendor
			if model_only:
				return model
			return vendor + '-' + model
		else:
			assert False, "no hdX or sdX"

	def free(self):
		try:
			mounts = open("/proc/mounts")
		except IOError:
			return -1

		lines = mounts.readlines()
		mounts.close()

		for line in lines:
			parts = line.strip().split(" ")
			real_path = path.realpath(parts[0])
			if not real_path[-1].isdigit():
				continue
			try:
				if MajorMinor(real_path) == MajorMinor(self.partitionPath(real_path[-1])):
					stat = statvfs(parts[1])
					return stat.f_bfree/1000 * stat.f_bsize/1000
			except OSError:
				pass
		return -1

	def numPartitions(self):
		numPart = -1
		if self.type == DEVTYPE_UDEV:
			try:
				devdir = listdir('/dev')
			except OSError:
				return -1
			for filename in devdir:
				if filename.startswith(self.device):
					numPart += 1

		elif self.type == DEVTYPE_DEVFS:
			try:
				idedir = listdir(self.dev_path)
			except OSError:
				return -1
			for filename in idedir:
				if filename.startswith("disc"):
					numPart += 1
				if filename.startswith("part"):
					numPart += 1
		return numPart

	def unmount(self, numpart = None):
		try:
			mounts = open("/proc/mounts")
		except IOError:
			return -1

		lines = mounts.readlines()
		mounts.close()

		cmd = "umount"
		for line in lines:
			parts = line.strip().split(" ")
			real_path = path.realpath(parts[0])
			if not real_path[-1].isdigit():
				if numpart is not None and numpart == 0:
					if real_path.startswith("/dev/sd"):
						uuid = harddiskmanager.getPartitionUUID(self.device)
						if uuid is not None:
							try:
								if MajorMinor(real_path) == MajorMinor(self.dev_path):
									cmd = ' ' . join([cmd, parts[1]])
									break
							except OSError:
								pass
			try:
				if MajorMinor(real_path) == MajorMinor(self.partitionPath(real_path[-1])):
					cmd = ' ' . join([cmd, parts[1]])
					break
			except OSError:
				pass
		res = system(cmd)
		if cmd == "umount": # nothing found to unmount
			res = 0
		return (res >> 8)

	def createPartition(self, numpart):
		#gpt partitiontype support currently disabled.
		"""devicename = self.device + "1"
		if numpart is not None:
			if numpart == 0:
				devicename = self.device
			if numpart >= 1:
				devicename = self.device + (str(numpart))
		type, sys, size, sizeg = harddiskmanager.getFdiskInfo(devicename)"""

		cmd = 'printf "8,\n;0,0\n;0,0\n;0,0\ny\n" | sfdisk -f -uS ' + self.disk_path

		#if sys is not None and "GPT" in sys:
		#	cmd = 'printf "34,,'+type+'\n;0,0\n;0,0\n;0,0\ny\n" | sfdisk -f -uS ' + self.disk_path

		res = system(cmd)
		return (res >> 8)

	def mkfs(self):
		cmd = "mkfs.ext3 "
		if self.diskSize() > 4 * 1024:
			cmd += "-T largefile "
		cmd += "-m0 -O dir_index " + self.partitionPath("1")
		res = system(cmd)
		return (res >> 8)

	def mount(self):
		try:
			fstab = open("/etc/fstab")
		except IOError:
			return -1

		lines = fstab.readlines()
		fstab.close()

		res = -1
		for line in lines:
			parts = line.strip().split(" ")
			real_path = path.realpath(parts[0])
			if not real_path[-1].isdigit():
				continue
			try:
				if MajorMinor(real_path) == MajorMinor(self.partitionPath(real_path[-1])):
					cmd = "mount -t auto " + parts[0]
					res = system(cmd)
					break
			except OSError:
				pass
		return (res >> 8)

	def createMovieFolder(self):
		try:
			makedirs(resolveFilename(SCOPE_HDD))
		except OSError:
			return -1
		return 0

	def fsck(self, numpart):
		# We autocorrect any failures and check if the fs is actually one we can check (currently ext2/ext3)
		partitionPath = self.partitionPath("1")
		if numpart is not None:
			if numpart == 0:
				partitionPath = self.dev_path
			if numpart >= 1:
				partitionPath = self.partitionPath(str(numpart))

		partitionType = harddiskmanager.getBlkidPartitionType(partitionPath)				

		res = -1
		if access(partitionPath, 0):
			if partitionType is not None and partitionType in ("ext2", "ext3"):
				cmd = "fsck." + partitionType + " -f -p " + partitionPath
				res = system(cmd)
		return (res >> 8)

	def killPartition(self, n):
		part = self.partitionPath("1")
		if n is not None:
			if n == 0:
				part = self.disk_path
			if n >= 1:
				part = self.partitionPath(str(n))
		if access(part, 0):
			#cmd = 'dd bs=512 count=3 if=/dev/zero of=' + part
			cmd = 'dd bs=4k count=3 if=/dev/zero of=' + part
			res = system(cmd)
		else:
			res = 0
		return (res >> 8)

	errorList = [ _("Everything is fine"), _("Creating partition failed"), _("Mkfs failed"), _("Mount failed"), _("Create movie folder failed"), _("Fsck failed"), _("Please Reboot"), _("Filesystem contains uncorrectable errors"), _("Unmount failed")]

	def initialize(self, isFstabMounted = False, numpart = None):
		if self.unmount(numpart) != 0:
			return -8
		# Udev tries to mount the partition immediately if there is an
		# old filesystem on it when fdisk reloads the partition table.
		# To prevent that, we overwrite the first sectors of the
		# partitions, if the partition existed before. This should work
		# for ext2/ext3 and also for GPT/EFI partitions.
		if numpart is not None:
			for p in range(numpart+1):
				self.killPartition(p)
		else:
			self.killPartition(1)

		if self.createPartition(numpart) != 0:
			return -1
		
		if self.mkfs() != 0:
			return -2

		if isFstabMounted:
			if self.mount() != 0:
				return -3
			if self.createMovieFolder() != 0:
				return -4

		return 0

	def check(self, isFstabMounted = False, numpart = None):
		
		if self.unmount(numpart) != 0:
			return -8

		res = self.fsck(numpart)
		if res & 2 == 2:
			return -6

		if res & 4 == 4:
			return -7

		if res != 0 and res != 1:
			# A sum containing 1 will also include a failure
			return -5
		
		if isFstabMounted:
			if self.mount() != 0:
				return -3

		return 0

	def getDeviceDir(self):
		return self.dev_path

	def getDeviceName(self):
		return self.disk_path

	# the HDD idle poll daemon.
	# as some harddrives have a buggy standby timer, we are doing this by hand here.
	# first, we disable the hardware timer. then, we check every now and then if
	# any access has been made to the disc. If there has been no access over a specifed time,
	# we set the hdd into standby.
	def readStats(self):
		try:
			l = open("/sys/block/%s/stat" % self.device).read()
		except IOError:
			return -1,-1
		(nr_read, _, _, _, nr_write) = l.split()[:5]
		return int(nr_read), int(nr_write)

	def startIdle(self):
		self.last_access = time.time()
		self.last_stat = 0
		self.is_sleeping = False
		from enigma import eTimer

		# disable HDD standby timer
		if self.bus() == "External":
			Console().ePopen(("sdparm", "sdparm", "--set=SCT=0", self.disk_path))
		else:
			Console().ePopen(("hdparm", "hdparm", "-S0", self.disk_path))
		self.timer = eTimer()
		self.timer.callback.append(self.runIdle)
		self.idle_running = True
		self.setIdleTime(self.max_idle_time) # kick the idle polling loop

	def runIdle(self):
		if not self.max_idle_time:
			return
		t = time.time()

		idle_time = t - self.last_access

		stats = self.readStats()
		print "nr_read", stats[0], "nr_write", stats[1]
		l = sum(stats)
		print "sum", l, "prev_sum", self.last_stat

		if l != self.last_stat and l >= 0: # access
			print "hdd was accessed since previous check!"
			self.last_stat = l
			self.last_access = t
			idle_time = 0
			self.is_sleeping = False
		else:
			print "hdd IDLE!"

		print "[IDLE]", idle_time, self.max_idle_time, self.is_sleeping
		if idle_time >= self.max_idle_time and not self.is_sleeping:
			self.setSleep()
			self.is_sleeping = True

	def setSleep(self):
		if self.bus() == "External":
			Console().ePopen(("sdparm", "sdparm", "--command=stop", self.disk_path))
		else:
			Console().ePopen(("hdparm", "hdparm", "-y", self.disk_path))

	def setIdleTime(self, idle):
		self.max_idle_time = idle
		if self.idle_running:
			if not idle:
				self.timer.stop()
			else:
				self.timer.start(idle * 100, False)  # poll 10 times per period.

	def isSleeping(self):
		return self.is_sleeping

class Partition:
	def __init__(self, mountpoint, device = None, description = "", force_mounted = False):
		self.mountpoint = mountpoint
		self.description = description
		self.force_mounted = force_mounted
		self.is_hotplug = force_mounted # so far; this might change.
		self.device = device
		self.disc_path = None
		self.uuid = None

	def stat(self):
		return statvfs(self.mountpoint)

	def free(self):
		try:
			s = self.stat()
			return s.f_bavail * s.f_bsize
		except OSError:
			return None

	def total(self):
		try:
			s = self.stat()
			return s.f_blocks * s.f_bsize
		except OSError:
			return None

	def mounted(self):
		# THANK YOU PYTHON FOR STRIPPING AWAY f_fsid.
		# TODO: can os.path.ismount be used?
		if self.force_mounted:
			return True

		try:
			mounts = open("/proc/mounts")
		except IOError:
			return False

		lines = mounts.readlines()
		mounts.close()

		for line in lines:
			if line.split(' ')[1] == self.mountpoint:
				return True
		return False


DEVICEDB_SR = \
	{"dm8000":
		{
			"/devices/pci0000:01/0000:01:00.0/host0/target0:0:0/0:0:0:0": _("DVD Drive"),
			"/devices/pci0000:01/0000:01:00.0/host1/target1:0:0/1:0:0:0": _("DVD Drive"),
			"/devices/platform/brcm-ehci-1.1/usb2/2-1/2-1:1.0/host3/target3:0:0/3:0:0:0": _("DVD Drive"),
		},
	"dm800":
	{
	},
	"dm7025":
	{
	}
	}

DEVICEDB = \
	{"dm8000":
		{
			"/devices/pci0000:01/0000:01:00.0/host1/target1:0:0/1:0:0:0": _("SATA"),
			"/devices/platform/brcm-ehci.0/usb1/1-1/1-1.1/1-1.1:1.0": _("Front USB"),
			"/devices/platform/brcm-ehci.0/usb1/1-1/1-1.1/1-1.1.": _("Front USB"),
			"/devices/platform/brcm-ehci.0/usb1/1-1/1-1.2/1-1.2:1.0": _("Back, upper USB"),
			"/devices/platform/brcm-ehci.0/usb1/1-1/1-1.2/1-1.2.": _("Back, upper USB"),
			"/devices/platform/brcm-ehci.0/usb1/1-1/1-1.3/1-1.3:1.0": _("Back, lower USB"),
			"/devices/platform/brcm-ehci.0/usb1/1-1/1-1.3/1-1.3.": _("Back, lower USB"),
			"/devices/platform/brcm-ehci-1.1/usb2/2-1/2-1:1.0/": _("Internal USB"),
			"/devices/platform/brcm-ohci-1.1/usb4/4-1/4-1:1.0/": _("Internal USB"),
			"/devices/platform/brcm-ehci.0/usb1/1-1/1-1.4/1-1.4.": _("Internal USB"),
		},
	"dm7020hd":
	{
		"/devices/pci0000:01/0000:01:00.0/host0/target0:0:0/0:0:0:0": _("SATA"),
		"/devices/pci0000:01/0000:01:00.0/host1/target1:0:0/1:0:0:0": _("eSATA"),
		"/devices/platform/brcm-ehci-1.1/usb2/2-1/2-1:1.0": _("Front USB"),
		"/devices/platform/brcm-ehci-1.1/usb2/2-1/2-1.": _("Front USB"),
		"/devices/platform/brcm-ehci.0/usb1/1-2/1-2:1.0": _("Back, upper USB"),
		"/devices/platform/brcm-ehci.0/usb1/1-2/1-2.": _("Back, upper USB"),
		"/devices/platform/brcm-ehci.0/usb1/1-1/1-1:1.0": _("Back, lower USB"),
		"/devices/platform/brcm-ehci.0/usb1/1-1/1-1.": _("Back, lower USB"),
	},
	"dm800":
	{
		"/devices/pci0000:01/0000:01:00.0/host0/target0:0:0/0:0:0:0": _("SATA"),
		"/devices/platform/brcm-ehci.0/usb1/1-2/1-2:1.0": _("Upper USB"),
		"/devices/platform/brcm-ehci.0/usb1/1-1/1-1:1.0": _("Lower USB"),
	},
	"dm800se":
	{
		"/devices/pci0000:01/0000:01:00.0/host0/target0:0:0/0:0:0:0": _("SATA"),
		"/devices/pci0000:01/0000:01:00.0/host1/target1:0:0/1:0:0:0": _("eSATA"),
		"/devices/platform/brcm-ehci.0/usb1/1-2/1-2:1.0": _("Upper USB"),
		"/devices/platform/brcm-ehci.0/usb1/1-1/1-1:1.0": _("Lower USB"),
	},
	"dm500hd":
	{
		"/devices/pci0000:01/0000:01:00.0/host1/target1:0:0/1:0:0:0": _("eSATA"),
		"/devices/pci0000:01/0000:01:00.0/host0/target0:0:0/0:0:0:0": _("eSATA"),
	},
	"dm7025":
	{
		"/devices/pci0000:00/0000:00:14.1/ide1/1.0": "Compact Flash", #hdc
		"/devices/pci0000:00/0000:00:14.1/ide0/0.0": "Internal Harddisk"
	}
	}

class HarddiskManager:
	def __init__(self):
		config.storage_options = ConfigSubsection()
		config.storage_options.default_device = ConfigText(default = "<undefined>")
		config.storage = ConfigSubDict()
		self.hdd = [ ]
		self.cd = ""
		self.partitions = [ ]
		self.devices_scanned_on_init = [ ]
		self.delayed_device_Notifier = [ ]

		self.on_partition_list_change = CList()
		
		# currently, this is just an enumeration of what's possible,
		# this probably has to be changed to support automount stuff.
		# still, if stuff is mounted into the correct mountpoints by
		# external tools, everything is fine (until somebody inserts
		# a second usb stick.)
		p = [
					("/media/hdd", _("Hard disk")),
					("/media/card", _("Card")),
					("/media/cf", _("Compact Flash")),
					("/media/mmc1", _("SD/MMC")),
					("/media/net", _("Network Mount")),
					("/media/ram", _("Ram Disk")),
					("/media/usb", _("USB Stick")),
					("/", _("Internal Flash"))
				]
		self.partitions.extend([ Partition(mountpoint = x[0], description = x[1]) for x in p ])

		self.setupConfigEntries(initial_call = True)

		self.enumerateBlockDevices()

	def getBlockDevInfo(self, blockdev):
		devpath = "/sys/block/" + blockdev
		error = False
		removable = False
		blacklisted = False
		is_cdrom = False
		partitions = []
		try:
			removable = bool(int(readFile(devpath + "/removable")))
			dev = int(readFile(devpath + "/dev").split(':')[0])
			if dev in (7, 31): # loop, mtdblock
				blacklisted = True
			if blockdev[0:2] == 'sr':
				is_cdrom = True
			if blockdev[0:2] == 'hd':
				try:
					media = readFile("/proc/ide/%s/media" % blockdev)
					if "cdrom" in media:
						is_cdrom = True
				except IOError:
					error = True
			# check for partitions
			if not is_cdrom:
				for partition in listdir(devpath):
					if partition[0:len(blockdev)] != blockdev:
						continue
					partitions.append(partition)
			else:
				self.cd = blockdev
		except IOError:
			error = True
		# check for medium
		medium_found = True
		try:
			open("/dev/" + blockdev).close()
		except IOError, err:
			if err.errno == 159: # no medium present
				medium_found = False
		return error, blacklisted, removable, is_cdrom, partitions, medium_found

	def enumerateBlockDevices(self):
		print "enumerating block devices..."
		for blockdev in listdir("/sys/block"):
			error, blacklisted, removable, is_cdrom, partitions, medium_found = self.addHotplugPartition(blockdev)
			if not error and not blacklisted:
				if medium_found:
					for part in partitions:
						self.addHotplugPartition(part)
				self.devices_scanned_on_init.append((blockdev, removable, is_cdrom, medium_found))
				print "[enumerateBlockDevices] devices_scanned_on_init:",self.devices_scanned_on_init

	def getAutofsMountpoint(self, device):
		return "/autofs/%s/" % (device)

	def is_hard_mounted(self, device):
		mounts = file('/proc/mounts').read().split('\n')
		for x in mounts:
			if x.find('/autofs') == -1 and x.find(device) != -1:
				#print "is_hard_mounted:",device
				return True
		return False

	def is_uuidpath_mounted(self, uuidpath, mountpoint):
		mounts = file('/proc/mounts').read().split('\n')
		for x in mounts:
			if not x.startswith('/'):
				continue
			path, mp = x.split()[0:2]
			if (path == uuidpath and mp == mountpoint):
				#print "is_uuid_mounted:'%s' for: %s " % (path, mp)
				return True
		return False

	def is_fstab_mountpoint(self, device, mountpoint):
		mounts = file('/etc/fstab').read().split('\n')
		for x in mounts:
			if not x.startswith('/'):
				continue
			dev, mp = x.split()[0:2]
			if (dev == device and mp == mountpoint):
				#print "is_fstab_mountpoint:'%s' for: %s " % (mp, dev)
				return True
		return False

	def get_fstab_mountstate(self, device, mountpoint):
		mounts = file('/etc/fstab').read().split('\n')
		for x in mounts:
			if not x.startswith('/'):
				continue
			dev, mp, ms = x.split()[0:3]
			if (dev == device and mp == mountpoint):
				#print "got_fstab_mountstate:'%s' for: %s - %s" % (ms, dev, mp)
				return ms
		return False

	def get_fstab_mountpoint(self, device):
		mounts = file('/etc/fstab').read().split('\n')
		for x in mounts:
			if not x.startswith('/'):
				continue
			dev, mp = x.split()[0:2]
			if device == dev:
				#print "got_fstab_mountpoint:'%s' for: %s" % (mp, dev)
				return mp
		return None

	def modifyFstabEntry(self, partitionPath, mountpoint, mode = "add_deactivated"):
		try:
			alreadyAdded = self.is_fstab_mountpoint(partitionPath, mountpoint)
			oldLine = None
			mounts = file('/etc/fstab').read().split('\n')
			fp = file("/etc/fstab", 'w')
			fp.write("#automatically edited by enigma2, " + str(time.strftime( "%a" + ", " + "%d " + "%b" + " %Y %H:%M:%S", time.localtime(time.time() ))) + "\n")
			for x in mounts:
				if (x.startswith(partitionPath) and mountpoint in x):
					oldLine = x
					continue
				if len(x):
					if x.startswith('#automatically'):
						continue
					fp.write(x + "\n")
			if not alreadyAdded:
				if mode == "add_deactivated":
					#print "modifyFstabEntry - add_deactivated:", partitionPath, mountpoint
					fp.write(partitionPath + "\t" + mountpoint + "\tnoauto\tdefaults\t0 0\n")
			else:
				if mode == "add_deactivated":
					if oldLine is not None:
						if "noauto" in oldLine:
							fp.write(oldLine + "\n")
						else:
							#print "modifyFstabEntry - add_deactivated - changed:", partitionPath, mountpoint
							fp.write(oldLine.replace("auto","noauto") + "\n")
			fp.close()
		except:
			print "error adding fstab entry for: %s" % (partitionPath)

	def addHotplugPartition(self, device, physdev = None):
		if not physdev:
			dev, part = self.splitDeviceName(device)
			try:
				physdev = path.realpath('/sys/block/' + dev + '/device')[4:]
			except OSError:
				physdev = dev
				print "couldn't determine blockdev physdev for device", device

		error, blacklisted, removable, is_cdrom, partitions, medium_found = self.getBlockDevInfo(device)
		print "found block device '%s':" % device,

		if blacklisted:
			print "blacklisted"
		else:
			if error:
				print "error querying properties"
			elif not medium_found:
				print "no medium"
			else:
				print "ok, removable=%s, cdrom=%s, partitions=%s" % (removable, is_cdrom, partitions)

			l = len(device)
			if l:
				# see if this is a harddrive or removable drive (usb stick/cf/sd)
				if not device[l-1].isdigit() and not is_cdrom:
					if self.getHDD(device) is None and medium_found:
						if removable:
							self.hdd.append(Harddisk(device, True))
						else:
							self.hdd.append(Harddisk(device, False))							
					self.hdd.sort()
					SystemInfo["Harddisk"] = len(self.hdd) > 0

				if (not removable or medium_found) and not self.is_hard_mounted(device):
					self.addDevicePartition(device, physdev)

		return error, blacklisted, removable, is_cdrom, partitions, medium_found

	def removeHotplugPartition(self, device):
		mountpoint = self.getAutofsMountpoint(device)
		uuid = self.getPartitionUUID(device)
		print "[removeHotplugPartition] for device:'%s'" % (device)
		p = self.getPartitionbyDevice(device)
		if p is None:
			p = self.getPartitionbyMountpoint(mountpoint)
		if p is not None:
			if uuid is None and p.uuid is not None:
				uuid = p.uuid
				harddiskmanager.unmountPartitionbyMountpoint(p.mountpoint)
			if uuid is not None and config.storage.get(uuid, None) is not None:
				self.unmountPartitionbyUUID(uuid)
				if not config.storage[uuid]['enabled'].value:
					del config.storage[uuid]
					print "[removeHotplugPartition] - remove uuid %s from temporary drive add" % (uuid)
			self.partitions.remove(p)
			self.on_partition_list_change("remove", p)

		if (uuid is not None and uuid == config.storage_options.default_device.value):
			self.verifyDefaultStorageDevice(uuid)
			from Tools import Notifications
			from Screens.MessageBox import MessageBox
			Notifications.AddNotification(MessageBox,_("Default storage device was removed!") + "\n" \
				+ _("Please verify if your default storage device is attached or set up your default storage device in menu -> setup -> system -> recording paths."), MessageBox.TYPE_INFO, timeout=20)

		l = len(device)
		if l and not device[l-1].isdigit():
			for hdd in self.hdd:
				if hdd.device == device:
					hdd.stop()
					self.hdd.remove(hdd)
					break
			SystemInfo["Harddisk"] = len(self.hdd) > 0

			#call the notifier only after we have fully removed the disconnected drive
			for callback in self.delayed_device_Notifier:
				try:
					callback(device, "remove_delayed" )
				except AttributeError:
					self.delayed_device_Notifier.remove(callback)

	def addDevicePartition(self, device, physdev):
		# device is the device name, without /dev
		# physdev is the physical device path, which we (might) use to determine the userfriendly name
		description = self.getUserfriendlyDeviceName(device, physdev)
		device_mountpoint = self.getAutofsMountpoint(device)
		uuid = self.getPartitionUUID(device)
		print "[addDevicePartition] device:'%s' with UUID:'%s'" % (device, uuid)
		if config.storage.get(uuid, None) is not None:
			if config.storage[uuid]['mountpoint'].value != "":
				device_mountpoint = config.storage[uuid]['mountpoint'].value
		if uuid is not None:
			if config.storage.get(uuid, None) is None:
				self.setupConfigEntries(initial_call = False, dev = device)
			else:
				self.storageDeviceChanged(uuid)
		p = self.getPartitionbyMountpoint(device_mountpoint)
		if p is not None:
			if uuid is not None:
				if p.uuid is not None and p.uuid != uuid:
					if config.storage.get(p.uuid, None) is not None:
						del config.storage[p.uuid] #delete old uuid reference entries
			p.mountpoint = device_mountpoint
			p.force_mounted = False
			p.device = device
			p.uuid = uuid
		else:
			forced = True
			if uuid is not None:
				cfg_uuid = config.storage.get(uuid, None)
				if cfg_uuid is not None:
					if cfg_uuid['enabled'].value:
						forced = False
					else:
						device_mountpoint = self.getAutofsMountpoint(device)
			x = self.getPartitionbyDevice(device)
			if x is None:
				p = Partition(mountpoint = device_mountpoint, description = description, force_mounted = forced, device = device)
				p.uuid = uuid
				self.partitions.append(p)
				self.on_partition_list_change("add", p)
			else:	# found old partition entry
				if config.storage.get(x.uuid, None) is not None:
					del config.storage[x.uuid] #delete old uuid reference entries
				x.mountpoint = device_mountpoint
				x.force_mounted = True
				x.uuid = uuid
		if p is not None:
			if uuid == config.storage_options.default_device.value:
				self.verifyDefaultStorageDevice()

		for callback in self.delayed_device_Notifier:
			try:
				callback(device, "add_delayed" )
			except AttributeError:
				self.delayed_device_Notifier.remove(callback)

	def HDDCount(self):
		return len(self.hdd)

	def HDDList(self):
		list = [ ]
		for hd in self.hdd:
			hdd = hd.model() + " - " + hd.bus()
			cap = hd.capacity()
			if cap != "":
				hdd += " (" + cap + ")"
			list.append((hdd, hd))
		return list

	def HDDEnabledCount(self):
		cnt = 0
		for uuid, cfg in config.storage.items():
			print "uuid", uuid, "cfg", cfg
			if cfg["enabled"].value:
				cnt += 1
		return cnt

	def getHDD(self, part):
		for hdd in self.hdd:
			if hdd.device == part[:3]:
				return hdd
		return None

	def getCD(self):
		return self.cd

	def getFdiskInfo(self, devname):
		size = sizeg = type = sys = None
		cmd = "fdisk -l /dev/" + devname

		try:
			for line in popen(cmd).read().split('\n'):
				if line.startswith("Disk"):
					sizeobj = re.search(r', ((?:[a-zA-Z0-9])*) bytes', line)
					if sizeobj:
						size = sizeobj.group(1)
					sizegobj = re.search(r': ((?:[0-9.0-9])*) GB', line)
					if sizegobj:
						sizeg = sizegobj.group(1)
				if not line.startswith('/'):
					continue
				if line.startswith("/dev/" + devname):
					a,b,c,d, type, sys = line.split(None,5)
		except:
			print "error getting fdisk device info"
		return type, sys, size, sizeg

	def getBlkidPartitionType(self, device):
		type = None
		if path.exists("/usr/sbin/blkid"):
			cmd = "/usr/sbin/blkid " + str(device)
			try:
				for line in popen(cmd).read().split('\n'):
					if not line.startswith(device):
						continue
					fstobj = re.search(r' TYPE="((?:[^"\\]|\\.)*)"', line)
					if fstobj:
						type = fstobj.group(1)
			except:
				print "error getting blkid partition type"
		return type

	def getLinkPath(self,link):
		if path.islink(link):
			p = path.normpath(readlink(link))
			if path.isabs(p):
				return p
			return path.join(path.dirname(link), p)

	def verifyDefaultStorageDevice(self, old_default = None):
		uuid = config.storage_options.default_device.value
		part = self.getdefaultStorageDevicebyUUID(uuid)
		defaultpath = "/media/hdd"
		curr_defaultpath = self.getLinkPath(defaultpath)
		if part is not None:
			if part in self.getConfiguredStorageDevices():
				mountpoint = part.mountpoint
				if path.ismount(mountpoint): #default storage mountpoint available ?
					if (path.exists(defaultpath) and not path.islink(defaultpath) and not path.ismount(defaultpath) ):
						try:
							rmdir(defaultpath)
							print "removed /media/hdd dir"
						except OSError:
							print "could not remove /media/hdd dir"
							try:
								rename(defaultpath,"/media/hdd_old")
								print "renamed /media/hdd dir to /media/hdd_old"
							except OSError:
								print "could not rename /media/hdd dir to /media/hdd_old"
					if (path.islink(defaultpath) and self.getLinkPath(defaultpath) != mountpoint ):
						try:
							unlink(defaultpath)
							print "removed old default storage link"
						except OSError:
							pass
					if not path.exists(defaultpath):
						try:
							symlink(mountpoint,defaultpath)
							print "default storage link created"
						except OSError:
							pass
					if self.getLinkPath(defaultpath) == mountpoint:
						print "default storage device successfully linked to /media/hdd"
				if not path.ismount(mountpoint): #default storage mountpoint unavailable !
					if self.getLinkPath(defaultpath) == mountpoint:
						try:
							unlink(defaultpath)
							print "removed unused /media/hdd"
						except OSError:
							print "could not remove unused /media/hdd link"
					if not path.exists(defaultpath):
						print "default storage link removed"
		else:
			if old_default is None:
				if uuid == "<undefined>":
					if (not path.islink(defaultpath) and not path.ismount(defaultpath) ):
						try:
							rmdir(defaultpath)
							print "removed undefined /media/hdd dir"
						except OSError:
							print "could not remove undefined /media/hdd dir"
							try:
								rename(defaultpath,"/media/hdd_old")
								print "renamed undefined /media/hdd dir to /media/hdd_old"
							except OSError:
								print "could not rename undefined /media/hdd dir to /media/hdd_old"
					if (path.islink(defaultpath) and not path.ismount(defaultpath)):
						try:
							unlink(defaultpath)
							print "removed undefined default storage link"
						except OSError:
							print "could not remove undefined /media/hdd dir"
				elif uuid != "<undefined>":
					cfg_uuid = config.storage.get(uuid, None)
					if cfg_uuid is not None and not cfg_uuid["enabled"].value:
						config.storage_options.default_device.value = "<undefined>"
						config.storage_options.default_device.save()
						config.storage_options.save()
						mountpoint = self.getLinkPath(defaultpath)
						uuidpath = "/dev/disk/by-uuid/" + uuid
						if (self.is_fstab_mountpoint(uuidpath, mountpoint) and self.get_fstab_mountstate(uuidpath, mountpoint) == 'noauto'):
							try:
								unlink(defaultpath)
								print "removed unconfigured storage device link"
							except OSError:
								pass
					elif cfg_uuid is not None and cfg_uuid["enabled"].value:
						print self.getLinkPath(defaultpath), cfg_uuid["mountpoint"].value
						if cfg_uuid["mountpoint"].value == self.getLinkPath(defaultpath):
							try:
								unlink(defaultpath)
								print "removed default storage link for offline device"
							except OSError:
								pass
			else:
				if uuid == old_default:
					cfg_uuid = config.storage.get(old_default, None)
					if cfg_uuid is not None and cfg_uuid["enabled"].value:
						print self.getLinkPath(defaultpath), cfg_uuid["mountpoint"].value
						if cfg_uuid["mountpoint"].value == self.getLinkPath(defaultpath):
							try:
								unlink(defaultpath)
								print "removed default storage link for offline device"
							except OSError:
								pass

	def defaultStorageDeviceChanged(self, mountpoint = None):
		part = self.getPartitionbyMountpoint(mountpoint)
		if (part is not None and part in self.getConfiguredStorageDevices()):
			config.storage_options.default_device.value = part.uuid
		if (part is not None and part not in self.getConfiguredStorageDevices()):
			config.storage_options.default_device.value = "<undefined>"
		if (part is None and mountpoint == "<undefined>"):
			config.storage_options.default_device.value = "<undefined>"
		print "defaultStorageDeviceChanged:",config.storage_options.default_device.value
		config.storage_options.default_device.save()
		config.storage_options.save()
		self.verifyDefaultStorageDevice()

	def isConfiguredStorageDevice(self,uuid):
		cfg_uuid = config.storage.get(uuid, None)
		if cfg_uuid is not None and cfg_uuid["enabled"].value:
			#print "isConfiguredStorageDevice:",uuid
			return True
		return False

	def getdefaultStorageDevicebyUUID(self, uuid):
		part = None
		for p in self.getConfiguredStorageDevices():
			if p.uuid == uuid:
				part = p
		return part

	def getConfiguredStorageDevices(self):
		parts = [x for x in self.partitions if (x.uuid is not None and x.mounted() and self.isConfiguredStorageDevice(x.uuid))]
		return [x for x in parts]

	def getMountedPartitions(self, onlyhotplug = False):
		parts = [x for x in self.partitions if (x.is_hotplug or not onlyhotplug) and x.mounted()]
		devs = set([x.device for x in parts])
		for devname in devs.copy():
			if not devname:
				continue
			dev, part = self.splitDeviceName(devname)
			if part and dev in devs: # if this is a partition and we still have the wholedisk, remove wholedisk
				devs.remove(dev)
		# return all devices which are not removed due to being a wholedisk when a partition exists
		return [x for x in parts if not x.device or x.device in devs]

	def splitDeviceName(self, devname):
		# this works for: sdaX, hdaX, sr0 (which is in fact dev="sr0", part=""). It doesn't work for other names like mtdblock3, but they are blacklisted anyway.
		dev = devname[:3]
		part = devname[3:]
		for p in part:
			if not p.isdigit():
				return devname, 0
		return dev, part and int(part) or 0

	def getUserfriendlyDeviceName(self, dev, phys):
		dev, part = self.splitDeviceName(dev)
		description = "External Storage %s" % dev
		have_model_descr = False
		try:
			description = readFile("/sys" + phys + "/model")
			have_model_descr = True
		except IOError, s:
			print "couldn't read model: ", s
		from Tools.HardwareInfo import HardwareInfo
		if dev.find('sr') == 0 and dev[2].isdigit():
			devicedb = DEVICEDB_SR
		else:
			devicedb = DEVICEDB
		for physdevprefix, pdescription in devicedb.get(HardwareInfo().device_name,{}).items():
			if phys.startswith(physdevprefix):
				if have_model_descr:
					description = pdescription + ' - ' + description
				else:
					description = pdescription
		# not wholedisk and not partition 1
		if part and part != 1:
			description += " (Partition %d)" % part
		return description

	def addMountedPartition(self, device, desc):
		already_mounted = False
		for x in self.partitions[:]:
			if x.mountpoint == device:
				already_mounted = True
		if not already_mounted:
			self.partitions.append(Partition(mountpoint = device, description = desc))

	def removeMountedPartition(self, mountpoint):
		for x in self.partitions[:]:
			if x.mountpoint == mountpoint:
				self.partitions.remove(x)
				self.on_partition_list_change("remove", x)

	def removeMountedPartitionbyDevice(self, device):
		p = self.getPartitionbyDevice(device)
		if p is not None:
			#print "[removeMountedPartitionbyDevice] '%s', '%s', '%s', '%s', '%s'" % (p.mountpoint,p.description,p.device,p.force_mounted,p.uuid)
			self.partitions.remove(p)
			self.on_partition_list_change("remove", p)

	def trigger_udev(self):
		# We have to trigger udev to rescan sysfs 
		cmd = "udevadm trigger"
		res = system(cmd)
		return (res >> 8)

	def getPartitionbyDevice(self, dev):
		for x in self.partitions[:]:
			if x.device == dev:
				#print "[getPartitionbyDevice] '%s', '%s', '%s', '%s', '%s'" % (x.mountpoint,x.description,x.device,x.force_mounted,x.uuid)
				return x
		return None

	def getPartitionbyMountpoint(self, mountpoint):
		for x in self.partitions[:]:
			if x.mountpoint == mountpoint:
				#print "[getPartitionbyMountpoint] '%s', '%s', '%s', '%s', '%s'" % (x.mountpoint,x.description,x.device,x.force_mounted,x.uuid)
				return x
		return None

	def getDeviceNamebyUUID(self, uuid):
		if path.exists("/dev/disk/by-uuid/" + uuid):
			return path.basename(path.realpath("/dev/disk/by-uuid/" + uuid))
		return None

	def getPartitionUUID(self, part):
		if not path.exists("/dev/disk/by-uuid"):
			return None
		for uuid in listdir("/dev/disk/by-uuid/"):
			if not path.exists("/dev/disk/by-uuid/" + uuid):
				return None			
			if path.basename(path.realpath("/dev/disk/by-uuid/" + uuid)) == part:
				#print "[getPartitionUUID] '%s' - '%s'" % (uuid, path.basename(path.realpath("/dev/disk/by-uuid/" + uuid)) )
				return uuid
		return None

	def reloadExports(self):
		if path.exists("/etc/exports") and path.exists("/usr/sbin/exportfs"):
			Console().ePopen(("exportfs -r"))

	def unmountPartitionbyMountpoint(self, mountpoint, device = None):
		if path.exists(mountpoint) and path.ismount(mountpoint):
			part = self.getPartitionbyMountpoint(mountpoint)
			if part is not None and part.uuid is not None:
				if part.uuid == config.storage_options.default_device.value:
					print part.uuid,config.storage_options.default_device.value
					if (path.exists("/media/hdd") and path.islink("/media/hdd") and self.getLinkPath("/media/hdd") == mountpoint):
						try:
							unlink("/media/hdd")
							print "removed old default storage link"
						except OSError:
							pass					
			cmd = "umount" + " " + mountpoint
			print "[unmountPartitionbyMountpoint] %s:" % (mountpoint)
			system(cmd)
			if (device is not None and not path.ismount(mountpoint)):
				self.removeMountedPartitionbyDevice(device)
			self.reloadExports()

	def unmountPartitionbyUUID(self, uuid):
		mountpoint = config.storage[uuid]['mountpoint'].value
		if mountpoint != "":
			if path.exists(mountpoint) and path.ismount(mountpoint):
				partitionPath = "/dev/disk/by-uuid/" + uuid
				mountpoint = config.storage[uuid]['mountpoint'].value
				if (self.is_hard_mounted(partitionPath) and self.is_fstab_mountpoint(partitionPath, mountpoint) and self.get_fstab_mountstate(partitionPath, mountpoint) == 'auto'):
					print "[unmountPartitionbyUUID] disabling external mounted config entry for %s:" % (mountpoint)
					config.storage[uuid]["enabled"].value = False
					config.storage.save()
				else:
					part = self.getPartitionbyMountpoint(mountpoint)
					if part is not None and part.uuid is not None:
						if part.uuid == config.storage_options.default_device.value:
							print part.uuid,config.storage_options.default_device.value
							if (path.exists("/media/hdd") and path.islink("/media/hdd") and self.getLinkPath("/media/hdd") == mountpoint):
								try:
									unlink("/media/hdd")
									print "removed old default storage link"
								except OSError:
									pass					
					cmd = "umount" + " " + mountpoint
					print "[unmountPartitionbyUUID] %s:" % (mountpoint)
					system(cmd)
				self.reloadExports()

	def mountPartitionbyUUID(self, uuid):
		if path.exists("/dev/disk/by-uuid/" + uuid):
			print "[mountPartitionbyUUID] for UUID:'%s'" % (uuid)
			cfg_uuid = config.storage.get(uuid, None)
			partitionPath = "/dev/disk/by-uuid/" + uuid
			mountpoint = cfg_uuid['mountpoint'].value
			dev = self.getDeviceNamebyUUID(uuid)
			devicepath = "/dev/" + str(dev)

			if (self.is_hard_mounted(partitionPath) or self.is_hard_mounted(devicepath)):
				#print "[mountPartitionbyUUID] - found possible Fstab mounted device:",partitionPath, devicepath, dev
				if (self.is_fstab_mountpoint(partitionPath, mountpoint) and self.get_fstab_mountstate(partitionPath, mountpoint) == 'auto'):
					cfg_uuid["enabled"].value = False
					config.storage.save()
				if (self.is_fstab_mountpoint(devicepath, mountpoint) and self.get_fstab_mountstate(devicepath, mountpoint) == 'auto'):
					cfg_uuid["enabled"].value = False
					config.storage.save()	

			if cfg_uuid['enabled'].value:
				if mountpoint != "":
					if not path.exists(mountpoint):
						try:
							makedirs(mountpoint)
						except OSError:
							print "[mountPartitionbyUUID] could not create mountdir:",mountpoint

					if path.exists(mountpoint) and not path.ismount(mountpoint):
						cmd = "mount -t auto /dev/disk/by-uuid/" + uuid + " " + mountpoint
						system(cmd)
						print "[mountPartitionbyUUID]:",cmd

					hdd = self.getHDD(dev)
					if hdd is not None:
						partitionType = self.getBlkidPartitionType(devicepath)
						if partitionType is not None and partitionType in ( "ext2", "ext3" ):
							moviedir = mountpoint + "/movie"
							if not path.exists(moviedir):
								try:
									makedirs(moviedir)
								except OSError:
									print "[mountPartitionbyUUID] could not create moviedir:",moviedir

					if path.ismount(mountpoint):
						dev = self.getDeviceNamebyUUID(uuid)
						if dev is not None:
							p = self.getPartitionbyMountpoint(mountpoint)
							if p is not None:
								x = self.getPartitionbyDevice(dev)
								if x is not None and x.mountpoint.startswith('/autofs'):
									self.removeMountedPartitionbyDevice(dev)
								p.mountpoint = mountpoint
								p.uuid = uuid
								p.device = dev
								p.force_mounted = False
							else:
								p = self.getPartitionbyDevice(dev)
								if p is not None:
									p.mountpoint = mountpoint
									p.uuid = uuid
									p.device = dev
									p.force_mounted = False
					else:
						print "[mountPartitionbyUUID] could not mount mountdir:",mountpoint
		else:
			print "[mountPartitionbyUUID] failed for UUID:'%s'" % (uuid)

	def storageDeviceChanged(self, uuid):
		if config.storage[uuid]["enabled"].value:
			self.mountPartitionbyUUID(uuid)
		else:
			self.unmountPartitionbyUUID(uuid)

	def setupConfigEntries(self, initial_call = False, dev = None):
		if initial_call and not dev:
			for uuid in config.storage.stored_values:
				print "[setupConfigEntries] initial_call for stored uuid:",uuid,config.storage.stored_values[uuid]
				config.storage[uuid] = ConfigSubDict()
				config.storage[uuid]["enabled"] = ConfigYesNo(default = False)
				config.storage[uuid]["mountpoint"] = ConfigText(default = "", visible_width = 50, fixed_size = False)
				config.storage[uuid]["device_description"] = ConfigText(default = "", visible_width = 50, fixed_size = False)
				config.storage[uuid]["device_info"] = ConfigText(default = "", visible_width = 50, fixed_size = False)
				config.storage[uuid]["isRemovable"] = ConfigBoolean(default = False)
				dev = self.getDeviceNamebyUUID(uuid)
				if dev is not None:
					p = self.getPartitionbyDevice(dev) or self.getPartitionbyMountpoint(config.storage[uuid]["mountpoint"].value)
					if p is None: # manually add partition entry
						physdev = path.realpath('/sys/block/' + dev[:3] + '/device')[4:]
						description = self.getUserfriendlyDeviceName(dev[:3], physdev)
						p = Partition(mountpoint = config.storage[uuid]["mountpoint"].value, description = description, force_mounted = False, device = dev)
						p.uuid = uuid
						self.partitions.append(p)
						self.on_partition_list_change("add", p)	
				if path.exists("/dev/disk/by-uuid/" + uuid):
					self.storageDeviceChanged(uuid)
		if dev is not None:
			uuid = self.getPartitionUUID(dev)
			if uuid is not None:
				if config.storage.get(uuid, None) is None: #new unconfigured device added
					print "[setupConfigEntries] new device add for '%s' with uuid:'%s'" % (dev, uuid)
					hdd = self.getHDD(dev)
					if hdd is not None:
						hdd_description = hdd.model()
						cap = hdd.capacity()
						if cap != "":
							hdd_description += " (" + cap + ")"
						device_info =  hdd.bus_description()
					else:
						device_info = dev
						hdd_description = "External Storage"
					config.storage[uuid] = ConfigSubDict()
					config.storage[uuid]["enabled"] = ConfigYesNo(default = False)
					config.storage[uuid]["mountpoint"] = ConfigText(default = "", visible_width = 50, fixed_size = False)
					config.storage[uuid]["device_description"] = ConfigText(default = "", visible_width = 50, fixed_size = False)
					config.storage[uuid]["device_info"] = ConfigText(default = "", visible_width = 50, fixed_size = False)
					config.storage[uuid]["isRemovable"] = ConfigBoolean(default = False)
					config.storage[uuid]["device_description"].setValue(hdd_description)
					config.storage[uuid]["device_info"].setValue(device_info)
					removable = False
					if hdd is not None:
						removable = hdd.isRemovable
					config.storage[uuid]["isRemovable"].setValue(removable)					
					p = self.getPartitionbyDevice(dev)
					if p is None: # manually add partition entry (e.g. on long spinup times)
						physdev = path.realpath('/sys/block/' + dev[:3] + '/device')[4:]
						description = self.getUserfriendlyDeviceName(dev[:3], physdev)
						p = Partition(mountpoint = self.getAutofsMountpoint(dev), description = description, force_mounted = True, device = dev)
						p.uuid = uuid
						self.partitions.append(p)
						self.on_partition_list_change("add", p)	
					self.storageDeviceChanged(uuid)		
				else:
					print "[setupConfigEntries] new device add for '%s' with uuid:'%s'" % (dev, uuid)
					self.storageDeviceChanged(uuid)
			else:
				"[setupConfigEntries] device add for '%s' without uuid !!!" % (dev)

harddiskmanager = HarddiskManager()