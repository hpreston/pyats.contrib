import os
import re
import sys
import time
import logging
import argparse
import ipaddress
from itertools import product
from collections import OrderedDict
from yaml import (YAMLError, safe_load)
from concurrent.futures import ThreadPoolExecutor


from genie.conf import Genie
from genie.testbed import load
from pyats.async_ import pcall
from genie.conf.base import Testbed, Device, Interface, Link
from genie.metaparser.util.exceptions import SchemaEmptyParserError

from .libs import testbed_manager
from .creator import TestbedCreator

log = logging.getLogger(__name__)

SUPPORTED_OS = ['nxos', 'iosxr', 'iosxe', 'ios']


class Topology(TestbedCreator):

    """ Topology class (TestbedCreator)

    Takes a yaml file given by argument testbed_file and attempts to connect
    to each device in the testbed and discover the devices connection using
    cdp and lldp and writes it to a new yaml file

    Args:
        testbed-file('str'): Mandatory argument - testbed-file
        config-discovery ('bool): if enabled the script will enable cdp and lldp on devices
                            and disable it afterwards default is false
        add-unconnected-interfaces('bool'): if enabled, script will add all interfaces to
                topology section of yaml file instead of just interfaces
                with active connections default is false
        exclude-network ('str'): list networks that won't be recorded by creator
                if found as part of a connection, default is that no ips
                will be excluded
                Example: <ipv4> <ipv4>
        exclude-interfaces ('str'):list interfaces that won't be recorded by creator
                if found as part of a connection, default is that no interfaces
                will be excluded
        only-links: Only find connections between already defined devices
                    in the testbed and will not discover new devices
                    default behavior is that device discovery will be done
        alias: takes argument in format device:alias device2:alias2 and
                and indicates which alias should be used to connect to the
                device first, default behavior has no prefered alias
        ssh_only: if True the script will only attempt to use ssh connections
                to connect to devices, default behavior is to use all connections
        timeout('int'): How long before connection and verification attempts time out.
                        default value is 10 seconds

    CLI Argument                                   |  Class Argument
    --------------------------------------------------------------------------------------------
    --testbed-file=value                           |  testbed_file=value
    --config-discovery                             |  config_discovery
    --add-unconnected-interfaces                   |  add_unconnected_interfaces=True
    --exclude-network='<ipv4> <ipv4>'              |  exclude_network='<ipv4> <ipv4>'  
    --exclude-interfaces='<interface> <interface>' |  exclude_interfaces='<interface> <interface>'
    --only-links                                   |  only_links=True
    --alias='<device>:<alias> <device>:<alias>'    |  alias='<device>:<alias> <device>:<alias>'
    --ssh_only                                     |  ssh_only=True
    --timeout=value                                |  timeout=value
    """

    def _init_arguments(self):
        """ Specifies the arguments for the creator.
        Returns:
            dict: Arguments for the creator.
        """
        # create 3 sets to track what devices have cdp and lldp configured
        # and what devices have be visited by the script
        self.alias_dict = {}
        return {
            'required': ['testbed_file'],
            'optional': {
                'config_discovery': False,
                'add_unconnected_interfaces': False,
                'exclude_network': '',
                'exclude_interfaces':'',
                'only_links': False,
                'timeout': 10,
                'alias': '',
                'ssh_only': False
            }
        }

    def _generate(self):
        """The _generate is called by the testbed creator - It starts here
        Takes testbed information and writes the topology information into
        a yaml file
        
        Returns:
            dict: The intermediate dictionary format of the testbed data.
        """

        # Load testbed file
        testbed = load(self._testbed_file)

        # Re-open the testbed file as yaml so we can read the 
        # connection password - so we can re-create the yaml
        # with these credential
        with open(self._testbed_file, 'r') as stream:
            try:
                testbed_yaml = safe_load(stream)
            except YAMLError as exc:
                log.error('error opening yaml file: {}'.format(exc))
        # Standardizing exclude networks
        exclude_networks = []
        for network in self._exclude_network.split():
            try:
                exclude_networks.append(ipaddress.ip_network(network))
            except Exception:
                raise Exception('IP range given {ip} is not valid'.format(ip=network))

        # take aliases entered by user and format it into dictionary
        for alias_mapping in self._alias.split():
           spli = alias_mapping.split(':')
           if len(spli) != 2:
               raise Exception('{} is not valid entry'.format(alias_mapping))
           self.alias_dict[spli[0]] = spli[1]

        dev_man = testbed_manager.TestbedManager(testbed, config=self._config_discovery,
                                                 ssh_only=self._ssh_only,
                                                 alias_dict=self.alias_dict,
                                                 timeout=self._timeout,
                                                 supported_os=SUPPORTED_OS)

        # Get the credential for the device from the yaml - so can recreate the
        # yaml with those
        credential_dict, proxy_set = dev_man.get_credentials_and_proxies(testbed_yaml)
        device_list = {}

        while len(testbed.devices) > len(dev_man.visited_devices):
            # connect to unvisited devices
            dev_man.connect_all_devices(len(testbed.devices))

            # Configure these connected devices
            if dev_man._config_discovery:
                dev_man.configure_testbed_cdp_protocol()
                dev_man.configure_testbed_lldp_protocol()

            # Get the cdp/lldp operation data and massage it into our structure format
            result = self.process_neigbor_data(testbed, device_list,
                                               exclude_networks, dev_man)

            log.info('Connections found in current set of devices: {}'.format(result))
            # add any new devices found to test bed
            new_devices = self._write_devices_into_testbed(device_list, proxy_set,
                                             credential_dict, testbed)

            # add new devices to testbed
            for device in new_devices.values():
                testbed.add_device(device)

            # add the connections that were found to the topology
            self._write_connections_to_testbed(result, testbed)
            if self._only_links:
                break
            log.info('looping to check newly discovered devices')
        # get IP address for interfaces
        for device in testbed.devices.values():
            dev_man.get_interfaces_ipV4_address(device)

        # unconfigure devices that had settings changed
        pcall(dev_man.unconfigure_neighbor_discovery_protocols,
              device= testbed.devices.values()
        )
        self.create_yaml_dict(testbed, testbed_yaml, dev_man)

        return testbed_yaml

    def process_neigbor_data(self, testbed, device_list, exclude_networks , dev_man):
        '''Takes a testbed and processes the cdp and lldp data of every
        device on the testbed that has not yet been visited

        Args:
            testbed: testbed of devices that will be visited
            device_list: list of device with information about how to
                            connect and their existing interfaces
            exclude_networks : range of ip addresses whose connections won't be logged in the yaml
            dev_man: device manager object for interacting with devices

        Returns:
            {device:{interface with connection:{'dest_host': destination device,
                                                'dest_port': destination device port,
                                                'ip_address': ip address of connection found}}}
        '''
        dev_to_test = []

        # if the device has not been visited add it to the list of devices
        # to get data from and add it to set of devices that have been examined
        for device in testbed.devices:
            if device not in dev_man.visited_devices:
                dev_man.visited_devices.add(device)
                dev_to_test.append(testbed.devices[device])

        # use pcall to get cdp and lldp information for all accessible devices
        result = pcall(dev_man.get_neighbor_info, device = dev_to_test)
        conn_dict = {}

        # process the connection data retrieved from getting cdp and lldp neighbors
        # and write it into a dictionary of format
        # {device:{interface with connection:{'dest_host': destination device,
        #                                     'dest_port': destination device port,
        #                                     'ip_address': ip address of connection found}}}
        for entry in result:
            for device in entry:
                conn_dict[device] = self.get_device_connections(entry[device],
                                                                device, 
                                                                device_list, 
                                                                exclude_networks, 
                                                                testbed)

        return conn_dict

    def get_device_connections(self, data, device_name, device_list, exclude_networks , testbed):
        '''Take a device from a testbed and find all the adjacent devices.
        First it processes the devices cdp information and writes it into the dict
        then it processes the lldp information and adds new data to the dict

        Args:
            data: dict containing devices cdp and lldp information
            device_name: the device whose connections are being processed
            device_list: list of device with information about how to
                            connect and their existing interfaces
            exclude_networks : range of ip addresses whose connections won't be logged in the yaml
            testbed: testbed of devices, used to check if found device is already in testbed or not
            
        Returns:
            Dictionary containing the connection info found on currently accesible
            devices
        '''
        connection_dict = {}
        interface_filter = re.compile(r'.+?(?=\d)')

        # get and parse cdp information
        result = data.get('cdp', [])
        log.info('cdp neighbor information: {}'.format(result))
        if result:
            self._process_cdp_information(result, device_name, device_list,
                                          exclude_networks , testbed, connection_dict)

        # get and parse lldp information
        result = data.get('lldp', [])
        log.info('lldp neighbor information: {}'.format(result))
        if result and result['total_entries'] != 0:
            self._process_lldp_information(result, device_name, device_list,
                                           exclude_networks , testbed, connection_dict)
        # if device being visited doesn't have a given interface,
        # add the interface
        for interface in connection_dict:
            if interface not in testbed.devices[device_name].interfaces:
                type_name = interface_filter.match(interface)
                interface_a = Interface(interface,
                                        type = type_name[0].lower())
                interface_a.device = testbed.devices[device_name]
        return connection_dict

    def _process_cdp_information(self, result, device_name, device_list, exclude_networks ,
                                 testbed, connection_dict):
        '''TODO: consider moving to own module for processing data
        Process the cdp parser information and enters it into the
        connection_dict and the device_list

        Args:
            Result: The parser result for show cdp neighbors
            device_name: the device who's parser information is being examined
            device_list: list of device with information about how to
                            connect and their existing interfaces
            exclude_networks : range of ip addresses whose connections won't be logged in the yaml
            testbed: testbed of devices, used to check if found device is already in testbed or not
            connection_dict: Dictionary of connections to write info into
        '''
        # filter to strip out the domain name from the system name
        domain_filter = re.compile(r'^.*?(?P<hostname>[-\w]+)\s?')

        for index in result['index']:

            # get the relevant information
            connection = result['index'][index]
            dest_host = connection.get('system_name')
            if not dest_host:
                dest_host = connection.get('device_id')
            filtered_name = domain_filter.match(dest_host)
            if filtered_name:
                dest_host = filtered_name.groupdict()['hostname']
            if self._only_links and dest_host not in testbed.devices:
                log.info('Device {} does not exists in {}, skipping'.format(
                    dest_host, testbed.name))
                continue

            dest_port = connection['port_id']
            interface = connection['local_interface']

            # if either the local or neigboring interface is in ignore list
            # do not log the connection on move on to the next one
            log.info('interface = {interface},'
                        'dest = {dest}'.format(
                            interface = interface, dest = dest_port))
            if interface in self._exclude_interfaces:
                log.info('connection interface {} is found in '
                        'ignore interface list, skipping connection'.format(
                            interface))
                continue
            if dest_port in self._exclude_interfaces:
                log.info('destination interface {} is found in '
                        'ignore interface list, skipping connection'.format(
                            dest_port))
                continue

            # get the ip addresses for the neighboring device
            mgmt_address = connection.get('management_addresses')
            int_address = connection.get('interface_addresses')
            int_set = {ip for ip in int_address}
            mgmt_set = {ip for ip in mgmt_address}

            # if the ip addresses for the connection are in the range given
            # by the cli, do not log the connection and move on
            stop = False
            for ip, net in product(int_set, exclude_networks ):
                if ipaddress.IPv4Address(ip) in net:
                    log.info('IP {ip} found in'
                            'ignored network {net}'.format(ip = ip, net = net))
                    stop = True
                    break
            for ip, net in product(mgmt_set, exclude_networks ):
                if ipaddress.IPv4Address(ip) in net:
                    log.info('IP {ip} found in'
                            'ignored network {net}'.format(ip = ip, net = net))
                    stop = True
                    break
            if stop:
                continue
            os = self.get_os(connection['software_version'],
                             connection['platform'])
            log.info('dest host = {}, dest port = {}, interface= {}, device = {}'.format(dest_host,dest_port, interface, device_name))
            # add the discovered information to
            # both the connection_dict and the device_list
            self.add_to_device_list(device_list, dest_host, dest_port, int_set, mgmt_set,
                                    os, device_name)
            self.add_to_connection_dict(connection_dict, dest_host, dest_port, interface, device_name)

    def _process_lldp_information(self, result, device_name, device_list, exclude_networks , testbed, connection_dict):
        '''TODO: consider moving to own module for processing data
        Process the lldp parser information and enters it into the
        connection_dict and the device_list

        Args:
            Result: The parser result for show lldp neighbors
            device_name: the device who's parser information is being examined
            device_list: list of device with information about how to
                            connect and their existing interfaces
            exclude_networks : range of ip addresses whose connections won't be logged in the yaml
            testbed: testbed of devices, used to check if found device is already in testbed or not
            connection_dict: Dictionary of connections to write info into
        '''
        # filter to strip out the domain name from the system name
        # Ex. n77-1.cisco.com becomes n77-1
        domain_filter = re.compile(r'^.*?(?P<hostname>[-\w]+)\s?')

        for interface, connection in result['interfaces'].items():
            port_list = connection['port_id']
            for dest_port in port_list:
                dest_host = list(port_list[dest_port]['neighbors'].keys())[0]
                if self._only_links and dest_host not in testbed.devices:
                    log.info('{} is not in initial testbed, '
                             'skipping connection'.format(dest_host))
                    continue
                neighbor = port_list[dest_port]['neighbors'][dest_host]

                # if either the local or neigboring interface is in ignore list
                # do not log the connection on move on to the next one
                log.info('interface = {interface}, '
                            'dest = {dest}'.format(interface = interface,
                                                dest = dest_port))
                if interface in self._exclude_interfaces:
                    log.info('connection interface {} is found in '
                            'ignore interface list,'
                            ' skipping connection'.format(interface))
                    continue
                if dest_port in self._exclude_interfaces:
                    log.info('destination interface {} is found in '
                            'ignore interface list,'
                            ' skipping connection'.format(dest_port))
                    continue

                # if the ip addresses for the connection are in the range given
                # by the cli, do not log the connection and move on
                ip_address = neighbor.get('management_address')
                if ip_address is None:
                    ip_address = neighbor.get('management_address_v4')
                if ip_address is not None and exclude_networks :
                    stop = False
                    for net in exclude_networks :
                        if ipaddress.IPv4Address(ip_address) in net:
                            log.info('IP {ip} found in ignored '
                                        'network {net}'.format(ip = ip_address,
                                                            net = net))
                            stop = True
                            break
                    if stop:
                        continue

                os = self.get_os(neighbor['system_description'], '')
                filtered_name = domain_filter.match(dest_host)
                if filtered_name:
                    dest_host = filtered_name.groupdict()['hostname']
                log.info('dest host = {}, dest port = {}, interface= {}, device = {}'.format(dest_host,dest_port, interface, device_name))
                # add the discovered information to both
                # the connection_dict and the device_list
                self.add_to_device_list(device_list, dest_host, dest_port,
                                        set(), {ip_address}, os, device_name)
                self.add_to_connection_dict(connection_dict, dest_host, dest_port, interface, device_name)

    def write_proxy_chain(self, finder_name, testbed, credentials, ip):
        '''creates a set of proxies for ssh connections, creating a set of
        commands if there are mutiple proxies involved

        Args:
            finder_name = name of device being used as proxy
            testbed = testbed where device is found
            credentials = credentials of finder device
            ip = interface ip used in connection
        '''
        finder_device = testbed.devices[finder_name]
        user = credentials['default']['username']
        for conn in finder_device.connections:
            if conn == 'defaults':
                continue
            connection_detail = finder_device.connections[conn]
            if connection_detail.protocol =='ssh' and 'proxy' in connection_detail:
                new_proxy = connection_detail.proxy
                conn_ip = connection_detail.ip
                break
        else:
            new_proxy = None
        if new_proxy is None:
            return finder_name
        if isinstance(new_proxy, list):
            new_proxy[-1]['command'] = 'ssh {user}@{ip}'.format(user = user, ip = conn_ip)
            new_proxy.append({'device': finder_name, 'command': 'ssh {user}@{ip}'.format(user = user, ip = ip)})
            return new_proxy
        if isinstance(new_proxy,str):
            proxy_steps = [{'device':new_proxy,'command':'ssh {}'.format(conn_ip)},
                           {'device':finder_name, 'command': 'ssh {user}@{ip}'.format(user = user, ip = ip)}]
            return proxy_steps

    def add_to_device_list(self, device_list, dest_host,
                           dest_port, int_address, mgmt_address, os, discover_name):
        '''TODO: consider moving to own module for processing data
        Add the information needed to create the device in the
        testbed later to the specified list
        Args:
            device_list: list of device with information about how to
                            connect and their existing interfaces
            dest_host: device being added to the list
            dest_port: interface of device to be added
            ip_address: ip address of the device found
            os: the os of the device
            discover_name: the name of the device that discovered dest_host
        '''
        int_address.difference_update(mgmt_address)
        if dest_host not in device_list:
            device_list[dest_host] = {'ports': {dest_port},
                                    'ip':mgmt_address,
                                    'os': os,
                                    'finder': (discover_name, int_address)}
        else:
            if device_list[dest_host]['os'] is None:
                device_list[dest_host]['os'] = os
            device_list[dest_host]['ports'].add(dest_port)
            device_list[dest_host]['ip'] = device_list[dest_host]['ip'].union(mgmt_address)

    def add_to_connection_dict(self, connection_dict,
                               dest_host, dest_port,interface, dev):
        '''TODO: consider moving to own module for processing data
        Adds the information about a connection to be added to the topology
        recording what device interface combo is connected to the given
        interface and ip address involved in the connection
        
        Args:
            connection_dict: Dictionary of connections to write info into
            dest_host: device at other end of connection
            dest_port: interface used by dest_host in connection
            ip_address: ip_address involved in connection
            interface: interface of device used in connection
            dev: the device involved in the connection
        '''
        new_entry = {'dest_host': dest_host,
                    'dest_port': dest_port}
        if interface not in connection_dict:
            connection_dict[interface] = [new_entry]
        else:
            for entry in connection_dict[interface]:

                # check that the connection being added is unique
                if (entry['dest_host'] == dest_host
                        and entry['dest_port'] == dest_port):
                    break
            else:
                log.info('Connection device {} interface {} to'
                         ' device {} interface {} found'.format(dev,
                                                                interface,
                                                                dest_host,
                                                                dest_port))
                connection_dict[interface].append(new_entry)

    def get_os(self, system_string, platform_name):
        '''Get the os from the system_description output from the show
        cdp and show lldp neighbor parsers

        Args:
            system_string: posible location for os name
            platform_name: possible location for os name
        Return:
            returns os or None
        '''
        if 'IOS' in system_string or 'IOS' in platform_name:
            if 'XE' in system_string or 'XE' in platform_name:
                return 'iosxe'
            elif 'XR' in system_string or 'XR' in platform_name:
                return 'iosxr'
            else:
                return 'ios'
        if 'NX-OS' in system_string or 'NX-OS' in platform_name:
            return 'nxos'

    def create_yaml_dict(self, testbed, testbed_yaml, dev_man):
        '''Take the information laid out in the testbed and then format it into a
        dictionary to be integrated with the existing

        Args:
            testbed: testbed whose devices and connections are added
            testbed_yaml: exisiting yaml file that will have the new data added to it
            dev_man: device manager object used to get credentials and proxies
        Returns:
            dictionay in yaml format
        '''
        log.info('Creating dictionary based on testbed')
        yaml_dict = {'devices':{}, 'topology': {}}
        credential_dict, _ = dev_man.get_credentials_and_proxies(testbed_yaml)

        # write new devices into dict
        for device in testbed.devices.values():
            log.info('Adding device info for {}'.format(device.name))
            if device.name not in testbed_yaml['devices']:
                yaml_dict['devices'][device.name] = {'type': device.type,
                                                    'os': device.os,
                                                    'credentials': credential_dict,
                                                    'connections': {}
                                                    }
                conn_dict = yaml_dict['devices'][device.name]['connections']
                for connect in device.connections:
                    ip = device.connections[connect].get('ip')
                    protocol = device.connections[connect].get('protocol')
                    proxy = device.connections[connect].get('proxy')
                    conn_dict[connect] = {'protocol':protocol,
                                            'ip': ip,
                                          'proxy': proxy
                                         }
                conn_dict['defaults'] = {'via':connect}


            # write in the interfaces and link from devices into testbed
            interface_dict = {'interfaces': {}}
            log.info('Adding interface info for {}'.format(device.name))
            for interface in device.interfaces.values():
                interface_dict['interfaces'][interface.name] = {'type': interface.type}
                if interface.link is not None:
                    interface_dict['interfaces'][interface.name]['link'] = interface.link.name
                if interface.ipv4 is not None:
                    interface_dict['interfaces'][interface.name]['ipv4'] = str(interface.ipv4)
            # add interface information into the topology part of yaml_dict
            yaml_dict['topology'][device.name] = interface_dict
        log.info('topology discovered is: {}'.format(yaml_dict['topology']))

        # combine and add the new information to existing info
        log.info('combining existing testbed with new topology')
        for device in yaml_dict['devices']:
            if device not in testbed_yaml['devices']:
                testbed_yaml['devices'][device] = yaml_dict['devices'][device]
        if testbed_yaml.get('topology') is None:
            testbed_yaml['topology'] = yaml_dict['topology']
        else:
            for device in yaml_dict['topology']:
                if device not in testbed_yaml['topology']:
                    testbed_yaml['topology'][device] = yaml_dict['topology'][device]
                else:
                    testbed_yaml['topology'][device]['interfaces'].update(yaml_dict['topology'][device]['interfaces'])

    def _write_devices_into_testbed(self, device_list, proxy_set, credential_dict, testbed):
        ''' Writes any new devices found in the device list into the testbed
        and adds any missing interfaces into devices that are missing it
        TO DO: add option to create telnet connections

        Args:
            device_list: list of devices and attached interfaces to add to testbed
            proxy_set: list of proxies used by other devices in testbed
            credential_dict: Dictionary of credentials used by other devices in testbed
            testbed: testbed to add devices too

        Returns:
            Dictionary of new device objects to add to testbed
        '''
        # filter to strip out the numbers from an interface to create a type name
        # example: ethernet0/3 becomes ethernet
        interface_filter = re.compile(r'.+?(?=\d)')
        new_devices = {}

        for new_dev in device_list:
            if new_dev not in testbed.devices:
                log.info('New device {} found and'
                        ' being added to testbed'.format(new_dev))
                connections = {}
                # get credentials of finder device to use as new device credentials
                finder = device_list[new_dev]['finder']
                finder_dev = testbed.devices[finder[0]]
                credentials = finder_dev.credentials
                # create connections for the ip addresses in the device list
                for ip in device_list[new_dev]['ip']:
                    for proxy in proxy_set:
                        # create connection using possible proxies
                        connections['ssh' + proxy] = {
                            'protocol': 'ssh',
                            'ip': ip,
                            'proxy': proxy
                            }
                # create connection to device with given ip using a device that found it
                if device_list[new_dev]['finder'][1]:
                    finder_proxy = self.write_proxy_chain(
                                    finder[0], testbed, credentials, finder[1])
                    connections['finder_proxy'] = {
                            'protocol': 'ssh',
                            'ip': ip,
                            'proxy': finder_proxy
                            }
                # create the new device
                dev = Device(new_dev,
                            os = device_list[new_dev]['os'],
                            credentials = credentials,
                            type = 'device',
                            connections = connections,
                            custom = {'abstraction':
                                        {'order':['os'],
                                        'os': device_list[new_dev]['os']}
                                    })
                # create and add the interfaces for the new device
                for interface in device_list[new_dev]['ports']:
                    type_name = interface_filter.match(interface)
                    interface_a = Interface(interface,
                                            type = type_name[0].lower())
                    interface_a.device = dev
                new_devices[dev.name] = dev

            # if the device is already in the testbed, check if the interface exists or not
            else:
                if not self._add_unconnected_interfaces:
                    for interface in device_list[new_dev]['ports']:
                        if interface not in testbed.devices[new_dev].interfaces:
                            type_name = interface_filter.match(interface)
                            interface_a = Interface(interface,
                                                    type = type_name[0].lower())
                            interface_a.device = testbed.devices[new_dev]
                else:
                    try:
                        interface_list = testbed.devices[new_dev].parse('show interfaces description')
                    except Exception:
                        interface_list = testbed.devices[new_dev].parse('show interface description')
                    for interface in interface_list['interfaces']:
                        if interface not in testbed.devices[new_dev].interfaces:
                            type_name = interface_filter.match(interface)
                            interface_a = Interface(interface,
                                                    type = type_name[0].lower())
                            interface_a.device = testbed.devices[new_dev]
        return new_devices

    def _write_connections_to_testbed(self, connection_dict, testbed):
        '''Writes the connections found in the connection_dict into the testbed

        Args:
            connection_dict: Dictionary with connections found earlier
            testbed: testbed to write connections into
        '''
        log.info('Adding connections to testbed')
        for device in connection_dict:
            log.info('Writing connections found in {}'.format(device))
            for interface_name in connection_dict[device]:
                # get the interface found in the connection on the device searched
                interface = testbed.devices[device].interfaces[interface_name]
                # if the interface is not already part of a link get a list of
                # all interfaces involved in the link and create a new link
                # object with the associated interfaces
                if interface.link is None:
                    int_list = [interface]
                    name_set = {device}
                    for entry in connection_dict[device][interface_name]:
                        dev = entry['dest_host']
                        name_set.add(dev)
                        dest_int = entry['dest_port']
                        if testbed.devices[dev].interfaces[dest_int] not in int_list:
                            int_list.append(testbed.devices[dev].interfaces[dest_int])
                    link = Link('Link_{num}'.format(num = len(testbed.links)),
                                interfaces = int_list)

                # if the interface is already part of the link go over the
                # other interfaces found in the connection_dict and add them to the link
                # if they are not there already
                else:
                    link = interface.link
                    for entry in connection_dict[device][interface_name]:
                        dev = entry['dest_host']
                        dest_int = entry['dest_port']
                        if testbed.devices[dev].interfaces[dest_int] not in link.interfaces:
                            link.connect_interface(testbed.devices[dev].interfaces[dest_int])
        log.info('Finished writing connections')

