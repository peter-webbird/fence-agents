#!/usr/bin/python -tt

import sys, time
import shutil, tempfile, suds
import logging
import atexit
sys.path.append("@FENCEAGENTSLIBDIR@")

from suds.client import Client
from suds.sudsobject import Property
from fencing import *
from fencing import fail, EC_STATUS, EC_LOGIN_DENIED, EC_INVALID_PRIVILEGES, EC_WAITING_ON, EC_WAITING_OFF

#BEGIN_VERSION_GENERATION
RELEASE_VERSION="New VMWare Agent - test release on steroids"
REDHAT_COPYRIGHT=""
BUILD_DATE="April, 2011"
#END_VERSION_GENERATION

def soap_login(options):
	if options["--action"] in ["off", "reboot"]:
		time.sleep(int(options["--delay"]))

	if options.has_key("--ssl"):
		url = "https://"
	else:
		url = "http://"

	url += options["--ip"] + ":" + str(options["--ipport"]) + "/sdk"

	tmp_dir = tempfile.mkdtemp()
	tempfile.tempdir = tmp_dir
	atexit.register(remove_tmp_dir, tmp_dir)

	try:
		conn = Client(url + "/vimService.wsdl")
		conn.set_options(location = url)

		mo_ServiceInstance = Property('ServiceInstance')
		mo_ServiceInstance._type = 'ServiceInstance'
		ServiceContent = conn.service.RetrieveServiceContent(mo_ServiceInstance)
		mo_SessionManager = Property(ServiceContent.sessionManager.value)
		mo_SessionManager._type = 'SessionManager'

		conn.service.Login(mo_SessionManager, options["--username"], options["--password"])
	except Exception:
		fail(EC_LOGIN_DENIED)

	options["ServiceContent"] = ServiceContent
	options["mo_SessionManager"] = mo_SessionManager
	return conn

def process_results(results, machines, uuid, mappingToUUID):
	for m in results.objects:
		info = {}
		for i in m.propSet:
			info[i.name] = i.val
		# Prevent error KeyError: 'config.uuid' when reaching systems which P2V failed,
		# since these systems don't have a valid UUID
		if info.has_key("config.uuid"):
			machines[info["name"]] = (info["config.uuid"], info["summary.runtime.powerState"])
			uuid[info["config.uuid"]] = info["summary.runtime.powerState"]
			mappingToUUID[m.obj.value] = info["config.uuid"]

	return (machines, uuid, mappingToUUID)

def get_power_status(conn, options):
	mo_ViewManager = Property(options["ServiceContent"].viewManager.value)
	mo_ViewManager._type = "ViewManager"

	mo_RootFolder = Property(options["ServiceContent"].rootFolder.value)
	mo_RootFolder._type = "Folder"

	mo_PropertyCollector = Property(options["ServiceContent"].propertyCollector.value)
	mo_PropertyCollector._type = 'PropertyCollector'

	ContainerView = conn.service.CreateContainerView(mo_ViewManager, recursive = 1,
			container = mo_RootFolder, type = ['VirtualMachine'])
	mo_ContainerView = Property(ContainerView.value)
	mo_ContainerView._type = "ContainerView"

	FolderTraversalSpec = conn.factory.create('ns0:TraversalSpec')
	FolderTraversalSpec.name = "traverseEntities"
	FolderTraversalSpec.path = "view"
	FolderTraversalSpec.skip = False
	FolderTraversalSpec.type = "ContainerView"

	objSpec = conn.factory.create('ns0:ObjectSpec')
	objSpec.obj = mo_ContainerView
	objSpec.selectSet = [ FolderTraversalSpec ]
	objSpec.skip = True

	propSpec = conn.factory.create('ns0:PropertySpec')
	propSpec.all = False
	propSpec.pathSet = ["name", "summary.runtime.powerState", "config.uuid"]
	propSpec.type = "VirtualMachine"

	propFilterSpec = conn.factory.create('ns0:PropertyFilterSpec')
	propFilterSpec.propSet = [ propSpec ]
	propFilterSpec.objectSet = [ objSpec ]

	try:
		raw_machines = conn.service.RetrievePropertiesEx(mo_PropertyCollector, propFilterSpec)
	except Exception:
		fail(EC_STATUS)

	(machines, uuid, mappingToUUID) = process_results(raw_machines, {}, {}, {})

        # Probably need to loop over the ContinueRetreive if there are more results after 1 iteration.
	while hasattr(raw_machines, 'token'):
		try:
			raw_machines = conn.service.ContinueRetrievePropertiesEx(mo_PropertyCollector, raw_machines.token)
		except Exception:
			fail(EC_STATUS)
		(more_machines, more_uuid, more_mappingToUUID) = process_results(raw_machines, {}, {}, {})
		machines.update(more_machines)
		uuid.update(more_uuid)
		mappingToUUID.update(more_mappingToUUID)
		# Do not run unnecessary SOAP requests
		if options.has_key("--uuid") and options["--uuid"] in uuid:
			break

	if ["list", "monitor"].count(options["--action"]) == 1:
		return machines
	else:
		if options.has_key("--uuid") == False:
			if options["--plug"].startswith('/'):
				## Transform InventoryPath to UUID
				mo_SearchIndex = Property(options["ServiceContent"].searchIndex.value)
				mo_SearchIndex._type = "SearchIndex"

				vm = conn.service.FindByInventoryPath(mo_SearchIndex, options["--plug"])

				try:
					options["--uuid"] = mappingToUUID[vm.value]
				except KeyError:
					fail(EC_STATUS)
				except AttributeError:
					fail(EC_STATUS)
			else:
				## Name of virtual machine instead of path
				## warning: if you have same names of machines this won't work correctly
				try:
					(options["--uuid"], _) = machines[options["--plug"]]
				except KeyError:
					fail(EC_STATUS)
				except AttributeError:
					fail(EC_STATUS)

		try:
			if uuid[options["--uuid"]] == "poweredOn":
				return "on"
			else:
				return "off"
		except KeyError:
			fail(EC_STATUS)

def set_power_status(conn, options):
	mo_SearchIndex = Property(options["ServiceContent"].searchIndex.value)
	mo_SearchIndex._type = "SearchIndex"
	vm = conn.service.FindByUuid(mo_SearchIndex, vmSearch = 1, uuid = options["--uuid"])

	mo_machine = Property(vm.value)
	mo_machine._type = "VirtualMachine"

	try:
		if options["--action"] == "on":
			conn.service.PowerOnVM_Task(mo_machine)
		else:
			conn.service.PowerOffVM_Task(mo_machine)
	except suds.WebFault, ex:
		if (str(ex).find("Permission to perform this operation was denied")) >= 0:
			fail(EC_INVALID_PRIVILEGES)
		else:
			if options["--action"] == "on":
				fail(EC_WAITING_ON)
			else:
				fail(EC_WAITING_OFF)

def remove_tmp_dir(tmp_dir):
	shutil.rmtree(tmp_dir)

def main():
	device_opt = [ "ipaddr", "login", "passwd", "web", "ssl", "notls", "port" ]

	atexit.register(atexit_handler)

	options = check_input(device_opt, process_input(device_opt))

	##
	## Fence agent specific defaults
	#####
	docs = { }
	docs["shortdesc"] = "Fence agent for VMWare over SOAP API"
	docs["longdesc"] = "fence_vmware_soap is an I/O Fencing agent \
which can be used with the virtual machines managed by VMWare products \
that have SOAP API v4.1+. \
\n.P\n\
Name of virtual machine (-n / port) has to be used in inventory path \
format (e.g. /datacenter/vm/Discovered virtual machine/myMachine). \
In the cases when name of yours VM is unique you can use it instead. \
Alternatively you can always use UUID to access virtual machine."
	docs["vendorurl"] = "http://www.vmware.com"
	show_docs(options, docs)

	logging.basicConfig(level=logging.INFO)
	logging.getLogger('suds.client').setLevel(logging.CRITICAL)

	##
	## Operate the fencing device
	####
	conn = soap_login(options)

	result = fence_action(conn, options, set_power_status, get_power_status, get_power_status)

	##
	## Logout from system
	#####
	try:
		conn.service.Logout(options["mo_SessionManager"])
	except Exception:
		pass

	sys.exit(result)

if __name__ == "__main__":
	main()
