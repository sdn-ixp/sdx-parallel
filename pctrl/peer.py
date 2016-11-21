#!/usr/bin/env python
#  Author:
#  Muhammad Shahbaz (muhammad.shahbaz@gatech.edu)
#  Rudiger Birkner (Networked Systems Group ETH Zurich)
#  Arpit Gupta (Princeton)


from threading import RLock
import time

import os
import sys
np = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if np not in sys.path:
    sys.path.append(np)
import util.log

from rib import LocalRIB
from bgp_route import BGPRoute


class BGPPeer(object):
    def __init__(self, id, asn, ports, peers_in, peers_out):
        self.id = id
        self.asn = asn
        self.ports = ports
        self.lock_items = {}
        self.logger = util.log.getLogger('P'+str(self.id)+'-peer')

        tables = [
            {'name': 'input', 'primary_keys': ('prefix', 'neighbor'), 'mappings': []},
            {'name': 'local', 'primary_keys': ('prefix',), 'mappings': []},
            {'name': 'output', 'primary_keys': ('prefix',), 'mappings': []}
        ]

        self.rib = LocalRIB(self.asn, tables)

        # peers that a participant accepts traffic from and sends advertisements to
        self.peers_in = peers_in
        # peers that the participant can send its traffic to and gets advertisements from
        self.peers_out = peers_out

    def update(self, route):
        origin = None
        as_path = None
        med = None
        atomic_aggregate = None
        communities = None

        route_list = []
        # Extract out neighbor information in the given BGP update
        neighbor = route["neighbor"]["ip"]

        if 'state' in route['neighbor'] and route['neighbor']['state'] == 'down':
            self.logger.debug("PEER DOWN - ASN " + str(self.asn))

            routes = self.get_routes('input', True, neighbor=neighbor)

            for route_item in routes:
                route_list.append({'withdraw': route_item})

            self.delete_all_routes('input')
            self.delete_all_routes('local')
            self.delete_all_routes('output')

        if 'update' in route['neighbor']['message']:
            if 'attribute' in route['neighbor']['message']['update']:
                attribute = route['neighbor']['message']['update']['attribute']

                origin = attribute['origin'] if 'origin' in attribute else ''

                as_path = attribute['as-path'] if 'as-path' in attribute else []

                med = attribute['med'] if 'med' in attribute else ''

                community = attribute['community'] if 'community' in attribute else ''
                communities = ''
                for c in community:
                    communities += ':'.join(map(str,c)) + " "

                atomic_aggregate = attribute['atomic-aggregate'] if 'atomic-aggregate' in attribute else ''

            if 'announce' in route['neighbor']['message']['update']:
                announce = route['neighbor']['message']['update']['announce']
                if 'ipv4 unicast' in announce:
                    for next_hop in announce['ipv4 unicast'].keys():
                        for prefix in announce['ipv4 unicast'][next_hop].keys():
                            announced_route = BGPRoute(prefix,
                                                       neighbor,
                                                       next_hop,
                                                       origin,
                                                       as_path,
                                                       communities,
                                                       med,
                                                       atomic_aggregate)
                            self.add_route('input', announced_route)

                            route_list.append({'announce': announce_route})

            elif 'withdraw' in route['neighbor']['message']['update']:
                withdraw = route['neighbor']['message']['update']['withdraw']
                if 'ipv4 unicast' in withdraw:
                    for prefix in withdraw['ipv4 unicast'].keys():
                        deleted_route = self.get_routes('input', False, prefix=prefix, neighbor=neighbor)
                        if deleted_route:
                            self.delete_route('input', prefix=prefix, neighbor=neighbor)
                            route_list.append({'withdraw': deleted_route})

        return route_list

    def decision_process(self, update):
        'Update the local rib with new best path'
        if 'announce' in update:

            # NOTES:
            # Currently the logic is that we push the new update in input rib and then
            # make the best path decision. This is very efficient. This is how it should be
            # done:
            # (1) For announcement: We need to compare between the entry for that
            # prefix in the local rib and new announcement. There is not need to scan
            # the entire input rib. The compare the new path with output rib and make
            # deicision whether to announce a new path or not.
            # (2) For withdraw: Check if withdraw withdrawn route is same as
            # best path in local, if yes, then delete it and run the best path
            # selection over entire input rib, else just ignore this withdraw
            # message.

            new_best_route = None

            announce_route = update['announce']
            prefix = announce_route.prefix

            current_best_route = self.get_routes('local', False, prefix=prefix)

            # decision process if there is an existing best route
            if current_best_route:
                # new route is better than existing
                if announce_route > current_best_route:
                    new_best_route = announce_route
                # if the new route is an update of the current best route and makes it worse, we have to rerun the
                # entire decision process
                elif announce_route < current_best_route \
                        and announce_route.neighbor == current_best_route.neighbor:
                    routes = self.get_routes('input', True, prefix=announce_route.prefix)
                    # TODO check if it is necessary to append the route as we it should already be in the RIB
                    routes.append(announce_route)
                    routes.sort(reverse=True)

                    new_best_route = routes[0]
            else:
                # This is the first time for this prefix
                new_best_route = announce_route

            if new_best_route:
                self.update_route('local', new_best_route)

        elif 'withdraw' in update:
            deleted_route = update['withdraw']
            prefix = deleted_route.prefix

            if deleted_route is not None:
                # Check if the withdrawn route is the current_best_route and update best route
                current_best_route = self.get_routes('local', False, prefix=prefix)
                if current_best_route:
                    if deleted_route.neighbor == current_best_route.neighbor:
                        self.delete_route('local', prefix=prefix)
                        routes = self.get_routes('input', True, prefix=prefix)
                        if routes:
                            routes.sort(reverse=True)
                            best_route = routes[0]
                            self.update_route('local', best_route)
                    else:
                        self.logger.debug("BGP withdraw for prefix "+str(prefix)+" has no impact on best path")
                else:
                    self.logger.error("Withdraw received for a prefix which wasn't even in the local table")

    def bgp_update_peers(self, updates, prefix_2_VNH_nrfp, prefix_2_FEC , VNH_2_vmac, ports):
        announcements = []
        new_FECs = []

        for update in updates:
            if 'announce' in update:
                prefix = update['announce'].prefix
            else:
                prefix = update['withdraw'].prefix

            prev_route = self.get_routes('output', False, prefix=prefix)

            best_route = self.get_routes('local', False, prefix=prefix)
            # there is a race condition: sometimes the get_route happens, before the best route is updated
            if not best_route:
                time.sleep(.1)
                best_route = self.get_routes('local', False, prefix=prefix)
            if not best_route:
                assert(best_route is None)

            if 'announce' in update:
                # Check if best path has changed for this prefix
                # store announcement in output rib

                self.update_route("output", best_route)
                vnh = prefix_2_FEC[prefix]['vnh']
                if vnh not in VNH_2_vmac:
                    new_FECs.append(prefix_2_FEC[prefix])
                if best_route:
                    # announce the route to each router of the participant
                    for port in ports:
                        # TODO: Create a sender queue and import the announce_route function
                        announcements.append(announce_route(port["IP"], prefix, vnh, best_route.as_path))
                else:
                    self.logger.error("Race condition problem for prefix: "+str(prefix))
                    continue

            elif 'withdraw' in update:
                # A new announcement is only needed if the best path has changed
                if best_route:
                    # store announcement in output rib
                    self.update_route("output", best_route)
                    vnh = prefix_2_FEC[prefix]['vnh']
                    if vnh not in VNH_2_vmac:
                        new_FECs.append(prefix_2_FEC[prefix])
                    for port in ports:
                        announcements.append(announce_route(port["IP"],
                                                            prefix,
                                                            vnh,
                                                            best_route.as_path))

                else:
                    "Currently there is no best route to this prefix"
                    if prev_route:
                        # Clear this entry from the output rib
                        self.delete_route("output", prefix=prefix)
                        for port in self.ports:
                            # TODO: Create a sender queue and import the announce_route function
                            announcements.append(withdraw_route(port["IP"],
                                                                prefix,
                                                                prefix_2_VNH_nrfp[prefix]))

        return new_FECs, announcements

    def get_lock(self, lock):
        if lock not in self.lock_items:
            self.lock_items[lock] = RLock()
        return self.lock_items[lock]

    def process_notification(self, route):
        if 'shutdown' == route['notification']:
            self.delete_all_routes('input')
            self.delete_all_routes('local')
            self.delete_all_routes('output')

    def add_route(self, table_name, bgp_route): # updated
        with self.get_lock(bgp_route.prefix):
            self.rib.add(table_name, bgp_route)
            self.rib.commit()

    def get_routes(self, table_name, all_entries, **kwargs):
        key_items = kwargs
        lock = key_items['prefix'] if 'prefix' in key_items else 'global'
        with self.get_lock(lock):
            return self.rib.get(table_name, key_items, all_entries)

    def update_route(self, table_name, bgp_route):
        with self.get_lock(bgp_route.prefix):
            self.rib.add(table_name, bgp_route)

    def delete_route(self, table_name, **kwargs):
        key_items = kwargs
        lock = key_items['prefix'] if 'prefix' in key_items else 'global'
        with self.get_lock(lock):
            self.rib.delete(table_name, key_items)
            self.rib.commit()

    def delete_all_routes(self, table_name, **kwargs):
        key_items = kwargs
        self.rib.delete(table_name, key_items)
        self.rib.commit()


def announce_route(neighbor, prefix, next_hop, as_path):
    msg = "neighbor " + neighbor + " announce route " + prefix + " next-hop " + str(next_hop)
    msg += " as-path [ ( " + ' '.join(str(ap) for ap in as_path) + " ) ]"
    return msg


def withdraw_route(neighbor, prefix, next_hop):
    msg = "neighbor " + neighbor + " withdraw route " + prefix + " next-hop " + str(next_hop)
    return msg


''' main '''
if __name__ == '__main__':

    mypeer = peer('172.0.0.22')

    route = '''{ "exabgp": "2.0", "time": 1387421714, "neighbor": { "ip": "172.0.0.21", "update": { "attribute": { "origin": "igp", "as-path": [ [ 300 ], [ ] ], "med": 0, "atomic-aggregate": false }, "announce": { "ipv4 unicast": { "140.0.0.0/16": { "next-hop": "172.0.0.22" }, "150.0.0.0/16": { "next-hop": "172.0.0.22" } } } } } }'''

    mypeer.update(route)

    print mypeer.filter_route('input', 'as_path', '300')
