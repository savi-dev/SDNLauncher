#!/usr/bin/env python

# Copyright (c) 2014 University of Toronto.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

'''
SDNLauncher.py
===============
This script parses both topology.py and config.py and sets up the user defined
topology. After the VMs are launched, it establishes the connections via Vxlan

@author: Khashayar Hossein Zadeh <k.hosseinzadeh@mail.utoronto.ca>

'''

# vim: tabstop=4 shiftwidth=4 softtabstop=4 expandtab */

import novaclient.v1_1.client as nclient
import novaclient.v1_1.shell as nshell
import time
import paramiko
import sys
import subprocess
from quantumclient.v2_0 import client as qclient
import smtplib
from quantumclient.quantum import v2_0 as quantumv20
from novaclient import exceptions

from config import user, password, auth_url 
from config import instance_name, key_name, private_key_file, pub_key, key_name
from config import image_name, flavor_name, sec_group_name, vm_user_name, wait_before_ssh
from config import tenant_name, region_name 

from keystoneclient.v2_0 import client as ksclient
import re
from prettytable import PrettyTable
from topology import topology, nodes


def print_msg(msg):
    #pass
    print msg

"""
Here we parse the 'topology' dictionary found inside topology.py and extract 
a list 'nodeList' which stores the names of every Node
"""
# parse the user topology
valuelist = []
for values in topology.values():
    for tuples in values:
        # we only want the hosts 
        if isinstance(tuples, tuple):
            if ((tuples[0][0] == 'h' or tuples[0][0] == 'H') and tuples[0] not in valuelist):
                valuelist.append(tuples[0])

numHosts = len(valuelist)
numSwitches = len(topology.keys())
numNodes = numHosts + numSwitches

# this list contains the switch names and host names
nodeList = []
# start off by appending the switche names
for key in topology.keys():
    if (key not in nodeList):
        nodeList.append(key)

# this list only holds the host names (used to append into the nodeList)
hostList = []
for values in topology.values():
    for tuples in values:
        if isinstance(tuples, tuple):
            if ((tuples[0][0] == 'h' or tuples[0][0] == 'H') and tuples[0] not in hostList):
                hostList.append(tuples[0])

hostList.sort()
for elem in hostList:
    nodeList.append(elem)


try:
    with open(private_key_file) as f:
        private_key = f.read()
except:
    print "cant open key file: %s" %private_key_file
    sys.exit(0)


# three functions needed for whale client connection
def _strip_version(endpoint):
        """Strip a version from the last component of an endpoint if present"""

        # Get rid of trailing '/' if present
        if endpoint.endswith('/'):
            endpoint = endpoint[:-1]
        url_bits = endpoint.split('/')
        # regex to match 'v1' or 'v2.0' etc
        if re.match('v\d+\.?\d*', url_bits[-1]):
            endpoint = '/'.join(url_bits[:-1])
        return endpoint


def _get_ksclient(**kwargs):
        """Get an endpoint and auth token from Keystone.

        :param username: name of user
        :param password: user's password
        :param tenant_id: unique identifier of tenant
        :param tenant_name: name of tenant
        :param auth_url: endpoint to authenticate against
        """
        return ksclient.Client(username=kwargs.get('username'),
                               password=kwargs.get('password'),
                               tenant_id=kwargs.get('tenant_id'),
                               tenant_name=kwargs.get('tenant_name'),
                               auth_url=kwargs.get('auth_url'),
                               insecure=kwargs.get('insecure'))

def _get_endpoint(client, **kwargs):
        """Get an endpoint using the provided keystone client."""

        service_type = kwargs.get('service_type') or 'configuration'
        endpoint_type = kwargs.get('endpoint_type') or 'publicURL'
        region = kwargs.get('region')
        if region is not None:
                endpoint = client.service_catalog.url_for(
                             attr='region',
                             filter_value=region, 
                             service_type=service_type,
                             endpoint_type=endpoint_type)
        else:
                endpoint = client.service_catalog.url_for(
                             service_type=service_type,
                             endpoint_type=endpoint_type)

        return _strip_version(endpoint)


def check_host(server, host):
        server.get()
        if hasattr(server, "OS-EXT-SRV-ATTR:host"):
                return getattr(server, "OS-EXT-SRV-ATTR:host") == host
        return False

def checkServer(server):
        server.get()
        if hasattr(server, "fault"):
            print_msg("error fault is " + str(getattr(server, "fault")) + "\n")

"""
This function takes in a switch name, in the format 'sw#', ex: 'sw1' and runs several
ovs-vsctl commands. It starts off by adding our bridge, then sets up a controller address which the
switch connects to (if it was specified as none inside the topology.py file, we do not implement this).

The two for loops inside this function establish a vxlan configuration for every node this switch connects to.
The first for loop takes care of the connections found inside the topology[sw#] = '.... establishes everything here....'
and the second for loop establishes the connections to that switch found in another switch's dictionary values.

Example:
topology['sw1'] = [('h1', '192.168.200.10'),'sw3']
topology['sw2'] = ['sw1']
topology['sw3'] = [('h2', '192.168.200.11')]

The first for loop establishes the vxlands for h1 and sw3 and the second for loop
establishes the connection to sw2
"""
def setupSwitch(switch):
        fixed_ip= fxdict[switch]
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(fixed_ip, username=vm_user_name, key_filename=private_key_file)
        time.sleep(15)
        # running the ovs commands
        if switch not in nodes:
            print "Switch %s was not defined in 'nodes', setting up using default ovs commands" % switch
        bridge_name = 'br1'
        if 'bridge_name' in nodes[switch]:
            bridge_name = nodes[switch]['bridge_name']
        ssh.exec_command("sudo ovs-vsctl add-br %s" % bridge_name)
        time.sleep(2)
        if 'contr_addr' in nodes[switch]:
            ssh.exec_command("sudo ovs-vsctl set-controller %s tcp:%s" % (bridge_name, nodes[switch]['contr_addr']))
            time.sleep(1)
        if 'int_ip' in nodes[switch]:
            int_ip_name = nodes[switch]['int_ip'][0]
            int_ip = nodes[switch]['int_ip'][1]
            ssh.exec_command("sudo ovs-vsctl add-port %s %s -- set interface %s type=internal" % (bridge_name,int_ip_name, int_ip_name))
            time.sleep(1)     
            ssh.exec_command("sudo ifconfig %s %s/24 up" %(int_ip_name, int_ip))
            time.sleep(1) 
        ssh.exec_command("sudo ovs-vsctl set-fail-mode %s secure" % bridge_name)
        time.sleep(1)
        ssh.exec_command("sudo ovs-vsctl set controller %s connection-mode=out-of-band"% bridge_name)
        time.sleep(1) 
        # this will hold the internal ip for use in the vxlan set up
        connectip = ''
        # this is used for the vxlan count and VLNI number (this must be the same on both sides)
        vlni = 0
        # used to implement a different VLNI number. For the cases of multiple connections to the same node
        vnlilist = []
        # this 'host' is every node consisting of triplette or switch name for that switch
        for host in topology[switch]:
            # handle hosts 
            if isinstance(host, tuple):
                vlni = int(host[0][1]) + int(switch[2]) + (2*numSwitches) + 10
                vlni += vnlilist.count(vlni)
                vnlilist.append(vlni)
                connectip = fxdict[host[0]]
            # handle switches
            else: 
                vlni = int(host[2]) + int(switch[2]) + 10
                vlni += vnlilist.count(vlni)
                vnlilist.append(vlni)
                connectip = fxdict[host]
            ssh.exec_command("sudo ovs-vsctl add-port %s vxlan%s -- set interface vxlan%s type=vxlan options:remote_ip=%s options:key=%s" % (bridge_name,vlni,vlni,connectip,vlni))
            time.sleep(1)
        # establishes all the other connections to this switch 
        for keys in topology.keys():
            for host in topology[keys]:
                if (host == switch):
                    connectip = fxdict[keys]
                    vlni = int(keys[2]) + int(switch[2]) + 10 
                    vlni += vnlilist.count(vlni)
                    vnlilist.append(vlni)
                    ssh.exec_command("sudo ovs-vsctl add-port %s vxlan%s -- set interface vxlan%s type=vxlan options:remote_ip=%s options:key=%s" % (bridge_name, vlni, vlni,connectip, vlni))
                    time.sleep(1)
        ssh.close()

"""
This function takes in a host name, in the format 'h#', ex: 'h1' and runs several
ovs-vsctl commands. It starts off by adding a bridge and internal port for every connection
to/from this host. The internal IP can be set to none, in this case we do not implement it

The for loop inside this function performs the exact same as the 2nd for loop inside setupSwitches
"""
def setupHosts(host):
        fixed_ip= fxdict[host]
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(fixed_ip, username=vm_user_name, key_filename=private_key_file)
        time.sleep(15)
        # running the ovs commands
        count = 0
        connectip = ''
        vlni = 0
        vnlilist = []
        for keys in topology.keys():
            # this 'hosts' is every node consisting of triplette or switch name for that switch
            for hosts in topology[keys]:
                # if true, this is a triplette belonging to this desired host
                if (hosts[0] == host):
                    # third element is the bridge name
                    try:
                        if hosts[2]:
                            bridge_name = hosts[2]
                    except:
                        bridge_name = 'br%s' % count
                    ssh.exec_command("sudo ovs-vsctl add-br %s" % bridge_name)
                    time.sleep(2)
                    ssh.exec_command("sudo ovs-vsctl add-port %s p%s -- set interface p%s type=internal" % (bridge_name,count, count))
                    time.sleep(1)
                    if (hosts[1] != "None" or hosts[1] != "none"):
                        ssh.exec_command("sudo ifconfig p%s %s/24 up" %(count, hosts[1]))
                        time.sleep(1)
                    connectip = fxdict[keys]
                    vlni = int(keys[2]) + int(host[1]) + (2*numSwitches) + 10
                    vlni += vnlilist.count(vlni)
                    vnlilist.append(vlni)
                    ssh.exec_command("sudo ovs-vsctl add-port %s vxlan%s -- set interface vxlan%s type=vxlan options:remote_ip=%s options:key=%s" % (bridge_name,vlni,vlni,connectip,vlni))
                    time.sleep(1)
                    count += 1
        ssh.close()


print "\n\n"
print "----------- NETWORK TOPOLOGY -----------\n"

"""
Give a nice print out of the topology when this script runs
"""
# sort through the switch names since they become scrambled inside a Dict
tempsortlist = []
tempsortlist = topology.keys()
tempsortlist.sort()
for item in tempsortlist:
    print item + ' connects to these nodes (bidirectional):  ', topology[item]
print "\n"


# holds the internal ips for each VM... key = node name, value = internal ip
fxdict= {}
# backup of the default parameters as specified inside the config.py file
fixedRegion_name = region_name
fixedimage_name = image_name
fixedflavor_name = flavor_name
fixedInstancename = instance_name

done = False

if True:       
        s1 = None
        quantum = None
        try:
            # this list holds the servers (vms)
            servers_list=[]
            # this list holds each node's pretty table object
            table_list = []
            finished_servers = []
            # launch all of the VMs without checking the active state
            for i in range(numNodes): 
                """
                Parse the 'nodes' dictionary as defined in the topology.py file
                and set the variables before that specific VM launches
                """
                nodeName = nodeList[i]
                server_name = None
                try:
                    if nodeName in nodes: 
                        u_name = nodes[nodeName].get('vm_user_name', vm_user_name)
                        server_name = nodes[nodeName].get('server', None)
                        region_name = nodes[nodeName].get('region', fixedRegion_name)
                        flavor_name = nodes[nodeName].get('flavor', fixedflavor_name)
                        image_name = nodes[nodeName].get('image', fixedimage_name)
                        instance_name = nodes[nodeName].get('name', fixedInstancename + "%s" % (nodeName))
                    else:
                        region_name = fixedRegion_name
                        flavor_name = fixedflavor_name
                        image_name = fixedimage_name
                        instance_name = fixedInstancename + "%s" % (nodeName)
                except:
                    print "\n\n --------- ERROR IN THE NODES DICTIONARY on key %s-------------" %nodeName
                    print " using defualt parameters to launch"
                    region_name = fixedRegion_name
                    flavor_name = fixedflavor_name
                    image_name = fixedimage_name
                    instance_name = fixedInstancename + "%s" % (nodeName)
                                    

                print_msg("\nLaunching VM %d/%d on region: %s" % (i+1, numNodes, region_name))
                c=nclient.Client(user, password, tenant_name, auth_url, region_name=region_name, no_cache=True)
                time.sleep(5)
            
                image1=nshell._find_image(c, image_name)
                flavor1=nshell._find_flavor(c, flavor_name)

                seclist=[]
                seclist.append(sec_group_name)

                secgroup=nshell._get_secgroup(c, sec_group_name)
                try:
                    c.security_group_rules.create(secgroup.id, "TCP", 22, 22, "10.0.0.0/8")
                except:
                    pass
                try:
                    c.security_group_rules.create(secgroup.id, "UDP", 4789, 4789, "10.0.0.0/8")
                except:
                    pass
                try:
                    c.security_group_rules.create(secgroup.id, "ICMP", -1, 255, "10.0.0.0/8")
                except:
                    pass

                #create quantum client for floating ip address creation/association and VM network
                quantum=qclient.Client(username=user, password=password, tenant_name=tenant_name, auth_url=auth_url, region_name=region_name)
                #look for network id of the external network
                _network_id = quantumv20.find_resourceid_by_name_or_id(quantum, 'network', tenant_name+'-net')
                v_nics=[]
                v_nic={}

                x = PrettyTable(["Property", "Value"])
                x.add_row(["VM name", instance_name])
                x.add_row(["VM number", i+1])
                x.add_row(["Network ID",_network_id])
                v_nic['net-id']=_network_id
                v_nic['v4-fixed-ip']=None
                v_nics.append(v_nic)
                hints={}
                if server_name:
                    hints['force_hosts']=server_name
                servers=c.servers.list()
                s1=None
                for server in servers:
                    if server.name == instance_name:
                        s1 = server
                        print "found"
                        break
                if s1 is None or s1.name != instance_name:
                    print "creating VM"
                    s1=c.servers.create(instance_name, image1, flavor1, key_name=key_name, security_groups=seclist, scheduler_hints=hints, nics=v_nics)
                #print s1
                x.add_row(["VM ID",s1.id])
                # note, here we do not have the internal ips. So we specify the server id with that node's name
                fxdict[nodeName] = s1.id
                servers_list.append(s1)
                table_list.append(x)
            print "********************************************************"
            print "please wait for a couple of more minutes and run ./GetInfomration.py to make sure VMs are ready"
            print "after VMs are ready, run SetupTopology.py to setup the topology links"
            print "********************************************************"
            print "Done. Now exiting."
            done = True
            sys.exit(0)
            # Wait until every VM has booted up. Checks the active/error state of the VMs
            fixed_ip = None
            srv_cnt = 0
            for i in range (1, 50):
                srv_cnt = 0
                for s1 in servers_list:
                    s1.get()
                    if s1.status == "ERROR":
                        srv_cnt += 1
                        if s1.id in finished_servers:
                           continue;
                        finished_servers.append(s1.id)
                        print_msg("server is in error")
                    elif s1.status == "ACTIVE":
                        srv_cnt += 1
                        if s1.id in finished_servers:
                           continue;
                        finished_servers.append(s1.id)
                        (s_net, s_ip)=s1.networks.popitem()
                        if fixed_ip is None:
                            fixed_ip = s_ip[0]
                if srv_cnt >= len(servers_list):
                    print_msg("All servers are done")
                    break    
                print_msg("server count is %s/%s " % (srv_cnt, numNodes))
                time.sleep(10)

            # This forloop updates our 'fxdict' dict and matches the internal ips with that node name
            tempcount = 0
            for s1 in servers_list:
                s1.get()
                if s1.status == "ACTIVE":
                   (s_net, s_ip)=s1.networks.popitem()
                   # add in the internal ip for that node 
                   for key in fxdict:
                        if (fxdict[key] == s1.id):
                            fxdict[key] = s_ip[0]            
                   checkServer(s1)                       #-----------------------------
                   table_list[tempcount].add_row(["Host",str(getattr(s1, "OS-EXT-SRV-ATTR:host"))])
                   table_list[tempcount].add_row(["Instance Name",str(getattr(s1, "OS-EXT-SRV-ATTR:instance_name"))])
                   table_list[tempcount].add_row(["Interal IP addr", s_ip[0]])
                   tempcount += 1

            # get a list of port id in quantum
            ports=quantum.list_ports()

            #look for the port is of the server port
            for port in ports['ports']:
                    ips=port['fixed_ips']
                    for ip in ips:
                        if ip['ip_address'] == fixed_ip:
                                pid=port['id']
                                break

            # loop over and print out the tables. They are in the pretty table format
            tempcount = 0
            for s1 in servers_list:
                print table_list[tempcount]
                print "\n"
                tempcount += 1

            #look for network id of the external network
            _network_id = quantumv20.find_resourceid_by_name_or_id(quantum, 'network', 'ext_net')

            if True:
                s1 = servers_list[-1]
                fixed_ip = fxdict.values()[-1]
                print_msg("waiting %d seconds before ssh test" %wait_before_ssh)
                time.sleep(wait_before_ssh)

                try:
                    for i in range (1, 5):
                        s1.get()
                        server_console_output = s1.get_console_output()
                        if s1.status == "ACTIVE" and ("Generation complete." not in server_console_output):
                            if fixed_ip not in s1.get_console_output():
                                print_msg("failed to get dhcp address %s \n" % fixed_ip)
                            if "waiting 120 seconds for a network device" in server_console_output:
                                print_msg("strange!: waiting 120 seconds for a network device")
                            #print_msg("Please be patient, waiting another %d seconds \n" % wait_before_ssh)
                            break
                        elif s1.status == "ACTIVE":
                            print_msg("Server console-log is fine: %s, %s, %s \n" %(s1.id, s1.name, fixed_ip))
                            if fixed_ip not in s1.get_console_output():
                            #shouldnt get here if key genration comepleted fine
                                print_msg("failed to get dhcp address %s \n" % fixed_ip)
                            break
                        time.sleep(wait_before_ssh)
                except:
                    msg = msg + "\n exception in console-log check\n"
                    pass

                #ping fixed ip and print output
                if fixed_ip is not None:
                    str2="ping to fixedip %s failed " % fixed_ip 
                    try:
                        str1=subprocess.check_output(['ping', '-c 3', fixed_ip])
                    except:
                        pass
                    try:
                        str1=subprocess.check_output(['ping', '-c 3', fixed_ip])
                        str2=str1
                    except:
                        pass
                    print_msg(str2)

                    #ssh to the server and execute a couple of commands for sanity test
                    print_msg("starting ssh test")
                    out1 = ""
                    out2 = ""
                    try:
                        ssh = paramiko.SSHClient()
                        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                        ssh.connect(fixed_ip, username=vm_user_name, key_filename=private_key_file)
                        time.sleep(15)
    
                        stdin, stdout, stderr = ssh.exec_command("uptime")
                        stdin.close()
                        out1=stdout.readlines()
                        print_msg("uptime output is: %s" % (''.join(out1)))
                        time.sleep(3)
    
                        stdin, stdout, stderr = ssh.exec_command("ping -c2 www.google.ca")
                        stdin.close()
                        out2=stdout.readlines()
                        print_msg("ping output is: %s" % (''.join(out2)))
                        ssh.close() 
                        # running the ovs commands
                    except:
                        print_msg("Ssh failed. If the edge is overloaded, allocate more time before the SSH check")
    
                    print "\nPlease wait roughly %s seconds as the VxLans are being set up\n" % (numNodes*30)
                        
                    # set up the controllers
                    # the value "switch" being passed in is in the form of 'sw#'
                    for switch in topology.keys():
                        setupSwitch(switch) 
                        
                    # set up the hosts
                    # the value "host" being passed in is in the form of 'h#'
                    for host in hostList:
                        setupHosts(host)    

                    print "All Finished, you can now access your VMs \n\n"
                        
        except:
           if done is False:
               print "Failed to launch VMs. Check your keystone credentials"
               raise
               
