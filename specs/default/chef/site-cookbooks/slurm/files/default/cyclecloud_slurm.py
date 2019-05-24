# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
#

import argparse
from collections import OrderedDict
from getpass import getpass
import json
import logging
from math import ceil, floor
import os
import sys
import time
from uuid import uuid4

from clusterwrapper import ClusterWrapper
from cyclecloud.client import Client, Record
from cyclecloud.model.NodeCreationRequestModule import NodeCreationRequest
from cyclecloud.model.NodeCreationRequestSetDefinitionModule import NodeCreationRequestSetDefinition
from cyclecloud.model.NodeCreationRequestSetModule import NodeCreationRequestSet
import slurmcc
from slurmcc import chaos_mode, custom_chaos_mode
import random


class Partition:
    
    def __init__(self, name, nodearray, machine_type, is_default, max_scaleset_size, vcpu_count, memory, max_vm_count):
        self.name = name
        self.nodearray = nodearray
        self.machine_type = machine_type
        self.is_default = is_default
        self.max_scaleset_size = max_scaleset_size
        self.vcpu_count = vcpu_count
        self.memory = memory
        self.max_vm_count = max_vm_count
        self.node_list = None


def fetch_partitions(cluster_wrapper, subprocess_module):
    '''
    Construct a mapping of SLURM partition name -> relevant nodearray information.
    There must be a one-to-one mapping of partition name to nodearray. If not, first one wins.
    '''
    partitions = OrderedDict()
    
    _, status_response = cluster_wrapper.get_cluster_status(nodes=True)
    
    for nodearray_status in status_response.nodearrays:
        nodearray_name = nodearray_status.name
        if not nodearray_name:
            logging.error("Name is not defined for nodearray. Skipping")
            continue
        
        nodearray_record = nodearray_status.nodearray
        if not nodearray_record:
            logging.error("Nodearray record is not defined for nodearray status. Skipping")
            continue
            
        slurm_config = nodearray_record.get("Configuration", {}).get("slurm", {})
        is_autoscale = slurm_config.get("autoscale", False)
        partition_name = slurm_config.get("partition", nodearray_name)
        
        if not is_autoscale:
            logging.warn("Nodearray %s does not define slurm.autoscale, skipping.", nodearray_name)
            continue
        
        machine_types = nodearray_record.get("MachineType", "")
        if isinstance(machine_types, basestring):
            machine_types = machine_types.split(",")
        
        if len(machine_types) > 1:
            logging.warn("Ignoring multiple machine types for nodearray %s", nodearray_name)
            
        machine_type = machine_types[0]
        if not machine_type:
            logging.warn("MachineType not defined for nodearray %s. Skipping", nodearray_name)
            continue
        
        if partition_name in partitions:
            logging.warn("Same partition defined for two different nodearrays. Ignoring nodearray %s", nodearray_name)
            continue
        
        bucket = None
        for b in nodearray_status.buckets:
            if b.definition.machine_type == machine_type:
                bucket = b
                break
        
        if bucket is None:
            logging.error("Invalid status response - missing bucket with machinetype=='%s', %s", machine_type, json.dumps(status_response))
            return 1
        
        vm = bucket.virtual_machine
        if not vm:
            logging.error("Invalid status response - missing virtualMachine definition with machinetype=='%s', %s", machine_type, json.dumps(status_response))
            return 1
        
        if bucket.max_count is None:
            logging.error("No max_count defined for  machinetype=='%s'. Skipping", machine_type)
            continue
        
        if bucket.max_count <= 0:
            logging.info("Bucket has a max_count <= 0, defined for machinetype=='%s'. Skipping", machine_type)
            continue
        
        max_scaleset_size = nodearray_record.get("Azure", {}).get("MaxScalesetSize", 40)
        
        partitions[partition_name] = Partition(partition_name,
                                               nodearray_name,
                                               machine_type,
                                               slurm_config.get("default_partition", False),
                                               max_scaleset_size,
                                               vm.vcpu_count,
                                               vm.memory,
                                               bucket.max_count)
        
        existing_nodes = []
        for node in status_response.nodes:
            if node.get("Template") == nodearray_name:
                existing_nodes.append(node.get("Name"))
        
        if existing_nodes:
            partitions[partition_name].node_list = _to_hostlist(subprocess_module, ",".join(existing_nodes))
    
    partitions_list = partitions.values()
    default_partitions = [p for p in partitions_list if p.is_default]
    if len(default_partitions) == 0:
        logging.warn("slurm.default_partition was not set on any nodearray.")
        if len(partitions_list) == 1:
            logging.info("Only one nodearray was defined, setting as default.")
            partitions_list[0].is_default = True
    elif len(default_partitions) > 1:
        logging.warn("slurm.default_partition was set on more than one nodearray!")
    
    return partitions


_cluster_wrapper = None


def _get_cluster_wrapper(username=None, password=None, web_server=None):
    global _cluster_wrapper
    if _cluster_wrapper is None:
        try:
            import jetpack.config as jetpack_config
        except ImportError:
            jetpack_config = {}
            
        cluster_name = jetpack_config.get("cyclecloud.cluster.name")
            
        config = {"verify_certificates": False,
                  "username": username or jetpack_config.get("cyclecloud.config.username"),
                  "password": password or jetpack_config.get("cyclecloud.config.password"),
                  "url": web_server or jetpack_config.get("cyclecloud.config.web_server"),
                  "cycleserver": {
                      "timeout": 60
                  }
        }
        
        client = Client(config)
        cluster = client.clusters.get(cluster_name)
        _cluster_wrapper = ClusterWrapper(cluster.name, cluster._client.session, cluster._client)
        
    return _cluster_wrapper


def _generate_slurm_conf(partitions, writer, subprocess_module):
    for partition in partitions.values():
        num_placement_groups = int(ceil(float(partition.max_vm_count) / partition.max_scaleset_size))
        default_yn = "YES" if partition.is_default else "NO"
        writer.write("PartitionName={} Nodes={} Default={} MaxTime=INFINITE State=UP\n".format(partition.name, partition.node_list, default_yn))
        
        def sort_by_node_index(nodename):
            try:
                return int(nodename.split("-")[-1])
            except Exception:
                return nodename
            
        all_nodes = sorted(_from_hostlist(subprocess_module, partition.node_list), key=sort_by_node_index) 
        
        for pg_index in range(num_placement_groups):
            start = pg_index * partition.max_scaleset_size
            end = min(partition.max_vm_count - 1, (pg_index + 1) * partition.max_scaleset_size)
            subset_of_nodes = all_nodes[start:end]
            
            node_list = _to_hostlist(subprocess_module, ",".join(subset_of_nodes))
            
            writer.write("Nodename={} Feature=cloud STATE=CLOUD CPUs={} RealMemory={}\n".format(node_list, partition.vcpu_count, int(floor(partition.memory * 1024))))


def generate_slurm_conf():
    subprocess = _subprocess_module()
    partitions = fetch_partitions(_get_cluster_wrapper(), subprocess)
    _generate_slurm_conf(partitions, sys.stdout, subprocess)
            

def _generate_switches(partitions):
    switches = OrderedDict()
    
    for partition in partitions.itervalues():
        num_placement_groups = int(ceil(float(partition.max_vm_count) / partition.max_scaleset_size))
        placement_group_base = "{}-{}-{}-pg".format(partition.name, partition.nodearray, partition.machine_type)
        pg_list = "{}[0-{}]".format(placement_group_base, num_placement_groups - 1)
        for pg_index in range(num_placement_groups): 
            start = pg_index * partition.max_scaleset_size + 1
            end = min(partition.max_vm_count, (pg_index + 1) * partition.max_scaleset_size)
            node_list = "{}-[{}-{}]".format(partition.nodearray, start, end)
            
            placement_group_id = placement_group_base + str(pg_index)
            parent_switch = "{}-{}".format(partition.name, partition.nodearray)
            switches[parent_switch] = switches.get(parent_switch, OrderedDict({"pg_list": pg_list,
                                                                   "children": OrderedDict()}))
            switches[parent_switch]["children"][placement_group_id] = node_list
            
    return switches
        

def _store_topology(switches, fw):
    for parent_switch, parent_switch_dict in switches.iteritems():
        
        fw.write("SwitchName={} Switches={}\n".format(parent_switch, parent_switch_dict["pg_list"]))
        for placement_group_id, node_list in parent_switch_dict["children"].iteritems():
            fw.write("SwitchName={} Nodes={}\n".format(placement_group_id, node_list))


def _generate_topology(partitions, writer):
    logging.info("Checking for topology updates")
    new_switches = _generate_switches(partitions)
    _store_topology(new_switches, writer)


def generate_topology():
    subprocess = _subprocess_module()
    partitions = fetch_partitions(_get_cluster_wrapper(), subprocess)
    return _generate_topology(partitions, sys.stdout)
        
        
def _shutdown(node_list, cluster_wrapper):
    for _ in range(30):
        try:
            cluster_wrapper.shutdown_nodes(names=node_list)
            return
        except Exception:
            logging.exception("Retrying...")
            time.sleep(60)
    return 1


def shutdown(node_list):
    return _shutdown(node_list, _get_cluster_wrapper())


def _resume(node_list, cluster_wrapper, subprocess_module):
    _, start_response = cluster_wrapper.start_nodes(names=node_list)
    _wait_for_resume(cluster_wrapper, start_response.operation_id, node_list, subprocess_module)


def _wait_for_resume(cluster_wrapper, operation_id, node_list, subprocess_module):
    updated_node_addrs = set()
    
    previous_states = {}
    
    nodes_str = ",".join(node_list[:5])
    omega = time.time() + 3600 
    
    while time.time() < omega:
        states = {}
        _, nodes_response = cluster_wrapper.get_nodes(operation_id=operation_id)
        for node in nodes_response.nodes:
            name = node.get("Name")
            state = node.get("State")
            
            if node.get("TargetState") != "Started":
                states["UNKNOWN"] = states.get("UNKNOWN", {})
                states["UNKNOWN"][state] = states["UNKNOWN"].get(state, 0) + 1
                continue
            
            private_ip = node.get("PrivateIp")
            if state == "Started" and not private_ip:
                state = "WaitingOnIPAddress"
            
            states[state] = states.get(state, 0) + 1
            
            if private_ip and name not in updated_node_addrs:
                cmd = ["scontrol", "update", "NodeName=%s" % name, "NodeAddr=%s" % private_ip, "NodeHostname=%s" % private_ip]
                logging.info("Running %s", " ".join(cmd))
                subprocess_module.check_call(cmd)
                updated_node_addrs.add(name)
                
        terminal_states = states.get("Started", 0) + sum(states.get("UNKNOWN", {}).itervalues()) + states.get("Failed", 0)
        
        if states != previous_states:
            states_messages = []
            for key in sorted(states.keys()):
                if key != "UNKNOWN":
                    states_messages.append("{}={}".format(key, states[key]))
                else:
                    for ukey in sorted(states["UNKNOWN"].keys()):
                        states_messages.append("{}={}".format(ukey, states["UNKNOWN"][key]))
                        
            states_message = " , ".join(states_messages)
            logging.info("OperationId=%s NodeList=%s: Number of nodes in each state: %s", operation_id, nodes_str, states_message)
            
        if terminal_states == len(nodes_response.nodes):
            break
        
        previous_states = states
        
        time.sleep(5)
        
    logging.info("OperationId=%s NodeList=%s: all nodes updated with the proper IP address. Exiting", operation_id, nodes_str)
        
        
def resume(node_list):
    cluster_wrapper = _get_cluster_wrapper()
    return _resume(node_list, cluster_wrapper)


def _create_nodes(partitions, cluster_wrapper):
    request = NodeCreationRequest()
    request.request_id = str(uuid4())
    nodearray_counts = {}
     
    for partition in partitions.itervalues():
        for index in range(partition.max_vm_count):
            placement_group_index = index / partition.max_scaleset_size
            placement_group = "%s-%s-pg%d" % (partition.name, partition.machine_type, placement_group_index)
            key = (partition.name, placement_group)
            nodearray_counts[key] = nodearray_counts.get(key, 0) + 1
        
    request.sets = []
     
    for key, instance_count in sorted(nodearray_counts.iteritems(), key=lambda x: x[0]):
        partition_name, pg = key
        partition = partitions[partition_name]
        
        request_set = NodeCreationRequestSet()
        
        request_set.nodearray = partition.nodearray
        
        request_set.placement_group_id = pg
        
        request_set.count = instance_count
        
        request_set.definition = NodeCreationRequestSetDefinition()
        request_set.definition.machine_type = partition.machine_type
        
        request_set.node_attributes = Record()
        request_set.node_attributes["StartAutomatically"] = False
        request_set.node_attributes["State"] = "Off"
        request_set.node_attributes["TargetState"] = "Terminated"
        
        request.sets.append(request_set)
    
    cluster_wrapper.create_nodes(request)
        

def create_nodes(node_list):
    cluster_wrapper = _get_cluster_wrapper()
    subprocess = _subprocess_module()
    partitions = fetch_partitions(cluster_wrapper, subprocess)
    _create_nodes(node_list, partitions, cluster_wrapper)
        
        
def _init_logging(logfile):
    import logging.handlers
    
    log_level_name = os.getenv('AUTOSTART_LOG_LEVEL', "DEBUG")
    log_file_level_name = os.getenv('AUTOSTART_LOG_FILE_LEVEL', "DEBUG")
    log_file = os.getenv('AUTOSTART_LOG_FILE', logfile)
    
    if log_file_level_name.lower() not in ["debug", "info", "warn", "error", "critical"]:
        log_file_level = logging.DEBUG
    else:
        log_file_level = getattr(logging, log_level_name.upper())
    
    logging.getLogger("requests.packages.urllib3.connectionpool").setLevel(logging.WARN)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARN)
    
    log_file = logging.handlers.RotatingFileHandler(log_file, maxBytes=1024 * 1024 * 5, backupCount=5)
    log_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    log_file.setLevel(log_file_level)
    
    logging.getLogger().addHandler(log_file)
    logging.getLogger().setLevel(logging.DEBUG)
    
    
def _sync_nodes(cluster_wrapper, subprocess_module, partitions):
    nodes = cluster_wrapper.get_nodes()["nodes"]
    existing_nodes = subprocess_module.check_output(["sinfo", "-N", "-O", "NODELIST", "--noheader"]).splitlines()
    existing_nodes = set([x.strip() for x in existing_nodes])
    
    cyclecloud_nodes = {}
    for node in nodes:
        if node["Template"] not in partitions:
            continue
        
        cyclecloud_nodes[node["Name"]] = node
        
    manual_nodes = set(cyclecloud_nodes.keys()) - existing_nodes
    _resume(manual_nodes, cluster_wrapper)
    

def sync_nodes():
    cluster_wrapper = _get_cluster_wrapper()
    subprocess = _subprocess_module()
    partitions = fetch_partitions(cluster_wrapper, subprocess)
    return _sync_nodes(cluster_wrapper, subprocess, partitions)


def _to_hostlist(subprocess_module, nodes):
    return subprocess_module.check_output(["scontrol", "show", "hostlist", nodes]).strip()


def _from_hostlist(subprocess_module, hostlist_expr):
    stdout = subprocess_module.check_output(["scontrol", "show", "hostnames", hostlist_expr])
    return [x.strip() for x in stdout.split()]


def _subprocess_module():
    import subprocess
    
    def raise_exception():
        raise random.choice([OSError, subprocess.CalledProcessError])("Random failure")
    
    class SubprocessModuleWithChaosMode:
        @custom_chaos_mode(raise_exception)
        def check_call(self, *args, **kwargs):
            return subprocess.check_call(*args, **kwargs)
        
        @custom_chaos_mode(raise_exception)
        def check_output(self, *args, **kwargs):
            return subprocess.check_output(*args, **kwargs)
        
    return SubprocessModuleWithChaosMode()

    
def main(argv=None):
    
    def hostlist(hostlist_expr):
        import subprocess
        return _from_hostlist(subprocess, hostlist_expr)
    
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    
    slurm_conf_parser = subparsers.add_parser("slurm_conf")
    slurm_conf_parser.set_defaults(func=generate_slurm_conf, logfile="slurm_conf.log")
    
    topology_parser = subparsers.add_parser("topology")
    topology_parser.set_defaults(func=generate_topology, logfile="topology.log")
    
    create_nodes_parser = subparsers.add_parser("create_nodes")
    create_nodes_parser.set_defaults(func=create_nodes, logfile="create_nodes.log")
    create_nodes_parser.add_argument("--node-list", type=_from_hostlist, default=["all"])
    
    resume_parser = subparsers.add_parser("resume")
    resume_parser.set_defaults(func=resume, logfile="resume.log")
    resume_parser.add_argument("--node-list", type=hostlist, required=True)
    
    resume_fail_parser = subparsers.add_parser("resume_fail")
    resume_fail_parser.set_defaults(func=shutdown, logfile="resume_fail.log")
    resume_fail_parser.add_argument("--node-list", type=hostlist, required=True)
    
    suspend_parser = subparsers.add_parser("suspend")
    suspend_parser.set_defaults(func=shutdown, logfile="suspend.log")
    suspend_parser.add_argument("--node-list", type=hostlist, required=True)
    
    sync_nodes_parser = subparsers.add_parser("sync_nodes")
    sync_nodes_parser.set_defaults(func=sync_nodes, logfile="sync_nodes.log")
    
    for conn_parser in [sync_nodes_parser, create_nodes_parser, topology_parser, slurm_conf_parser]:
        conn_parser.add_argument("--web-server")
        conn_parser.add_argument("--username")
        
    args = parser.parse_args(argv)
    _init_logging(args.logfile)
    
    if hasattr(args, "username"):
        password = None
        if args.username:
            password = getpass()
            
        _get_cluster_wrapper(args.username, password, args.web_server)
    
    kwargs = {}
    for argname in dir(args):
        if argname[0].islower() and argname not in ["logfile", "func", "username", "password", "web_server"]:
            kwargs[argname] = getattr(args, argname)
    
    args.func(**kwargs)


if __name__ == "__main__":
    main()
