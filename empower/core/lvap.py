#!/usr/bin/env python3
#
# Copyright (c) 2015, Roberto Riggio
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
#    * Neither the name of the CREATE-NET nor the
#      names of its contributors may be used to endorse or promote products
#      derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY CREATE-NET ''AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL CREATE-NET BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""EmPOWER Light Virtual Access Point (LVAP) class."""

from empower.datatypes.etheraddress import EtherAddress
from empower.core.resourcepool import ResourcePool
from empower.core.resourcepool import ResourceBlock
from empower.core.radioport import RadioPort
from empower.core.radioport import DownlinkPort
from empower.core.radioport import UplinkPort
from empower.core.virtualport import VirtualPort
from empower.core.ryu import RyuFlowEntry

import empower.logger
LOG = empower.logger.get_logger()


class LVAP(object):
    """ The EmPOWER Light Virtual Access Point

    One LVAP is created for every station probing the network (unless the MAC
    was blocked or if the MAC was not in the allowed list). An LVAP can be
    hosted by multiple WTPs. More preciselly an LVAP can be scheduled on one,
    and onyl one resource block on the downlink direction and on multiple
    resource blocks on the uplink direction. The downlink resource block is
    automatically the default uplink resource block. The default uplink
    resource block is the resource block in charge of generating WiFi acks.
    Additional uplink resource blocks do not generate acks but can
    opportunistically receive and forward traffic. An unbound LVAP, i.e. an
    LVAP not hosted by any  WTP, is not admissible. The association between
    LVAP and ResourceBlock(s) is called Port and models the parameters of the
    link between WTP and LVAP. The Port abstraction is used to specify control
    policies at the WTP level. Example are the rate control algorithm which
    cannot be managed at the controller level due to timing constraints. Due
    to implementation constraints uplink ports CAN be define but are ignored.

    Handover can be performed by setting the wtp property of an lvap object to
    another wtp, e.g.:

      lvap.wtp = new_wtp

    where lvap is an instance of LVAP object and new_wtp is an instance of wtp
    object. The controller takes care of removing the lvap from the old wtp and
    spawing a new lvap on the target wtp. Stats are cleared on handovers. The
    new_wtp must support the same channel and band of the old wtp otherwise no
    handover is performed.

    An handover can also be perfomed by assigning a valid RasourcePool to an
    LVAP block property. First build the intersection and available
    resource pools

      pool = wtp.supports & lvap.supports
      lvap.block = pool

    This results in one resource block in the pool to be configured as
    downlink resource block (and thus also as default uplink resource block)
    while the others will be configured as uplink resource blocks. The
    downlink resource block is the one returned by the pop() method.

    A short cut is also provided:

      lvap.scheduled_on = block

    The operation above will assign the default port policy to the LVAP.

    To change the port configuration:

      port = lvap.scheduled_on[block]
      port.tx_power = 20

    Where block is the ResourceBlock previously assigned. A new port
    configuration can be assigned in a single step with:

      lvap.scheduled_on[block] = port

    where port is an instance of the Port class.

    The last line will trigger a Port update message if the entry already
    exists. If the entry does not exists and there are no other entries in
    the structure, then a new entry will be created and an add LVAP massage
    will be sent before sending the port update message. If an entry is
    already available in the structure then a ValueError is raised.

    Applications can also manually delete resource blocks (this will trigger a
    del lvap message) and create new ones (this will trigger an add lvap
    message):

      del lvap.scheduled_on[old_block]
      lvap.scheduled_on[new_block] = port

    Attributes:
        addr: The client's MAC Address as an EtherAddress instance.
        tx_samples: array of 2-tuples of the TX'ed packets
        rx_samples: array of 2-tuples of the RX'ed packets
        bssid: The LVAP's MAC Address as an EtherAddress instance. This
          address is dynamically generated by the Access Controller.
          The BSSID is supposed to be unique in the entire network.
        ssids: The list of SSIDs to be broadcasted for this LVAP.
        assoc_id: association id for this LVAP (this cannot change
          after the LVAP has been spawned
        authentication_state: set to True if the LVAP has already completed
          the open authentication handshake .
        association_state: set to True if the LVAP has already completed
          the association handshake.
        ssid: The currently associated SSID.
        tx_samples: the transmitted packets
        rx_samples: the received packets
        supports: a set of Resource Blocks supported by the LVAP
        block: the resource blocks to which this LVAP is assigned.
        downlink: the resource block assigned to this LVAP on the downlink
          direction.
        uplink: the resource block assigned to this LVAP on the uplink
          direction.
        scheduled_on: alias for the downlink property
    """

    def __init__(self, addr, bssid_addr):

        # read only params
        self.addr = addr
        self.bssid = bssid_addr
        self.supports = ResourcePool()

        # the following parameters are only updated upon RX of a lvap status
        # update message from an agent
        self.authentication_state = False
        self.association_state = False

        # the following parameters are only updated by the controller, which
        # will then dispatch an add lvap message in order to propagate the
        # change to the agent
        self._ssids = []
        self._encap = EtherAddress("00:00:00:00:00:00")

        # the following parameters can be updated by both agent and
        # controller. The controller sets them when a client successfully
        # associate. The agent sets them upon disassociation.
        self._assoc_id = 0
        self._ssid = None

        # only one block supported, default block points to this
        self._downlink = DownlinkPort()

        # multiple blocks supported, no port configuration supported
        self._uplink = UplinkPort()

        # counters
        self.tx_samples = []
        self.rx_samples = []

        # rates statistics
        self.rates = {}

        # virtual ports (VNFs)
        self.__ports = {}

    def set_tables(self):
        """Set openflow tables."""

        if not self.wtp:
            return

        ryu = RyuFlowEntry()

        ryu.add_station_flows(self.addr,
                              self.wtp.addr,
                              self.ports[0].ovs_port_id)

    def clear_tables(self):
        """Clear openflow tables."""

        if not self.wtp:
            return

        ryu = RyuFlowEntry()

        ryu.remove_station_flows(self.addr, self.wtp.addr)

    def set_ports(self):
        """Set virtual ports."""

        self.__ports = {}

        if not self.wtp:
            return

        for port in self.wtp.ports.values():

            if port.iface != "empower0":
                continue

            virtual_port = VirtualPort(dpid=self.wtp.addr,
                                       ovs_port_id=port.port_id,
                                       virtual_port_id=0,
                                       hwaddr=port.hwaddr,
                                       iface=port.iface)

            # these are used by the overridden dict methods
            virtual_port.next.lvap = self
            virtual_port.next.port = virtual_port

            self.__ports[0] = virtual_port

    @property
    def ports(self):
        """Get the virtual ports."""

        return self.__ports

    @property
    def encap(self):
        """Get the encap."""

        return self._encap

    @encap.setter
    def encap(self, encap):
        """ Set the encap. """

        if encap is None:
            encap = EtherAddress("00:00:00:00:00:00")

        if self._encap == encap:
            return

        self._encap = encap

        for port in self.downlink.values():
            port.block.radio.connection.send_add_lvap(port.lvap,
                                                      port.block,
                                                      self.downlink.SET_MASK)

        for port in self.uplink.values():
            port.block.radio.connection.send_add_lvap(port.lvap,
                                                      port.block,
                                                      self.uplink.SET_MASK)

    @property
    def assoc_id(self):
        """Get the assoc_id."""

        return self._assoc_id

    @assoc_id.setter
    def assoc_id(self, assoc_id):
        """Set the assoc id."""

        if self._assoc_id == assoc_id:
            return

        self._assoc_id = assoc_id

        for port in self.downlink.values():
            port.block.radio.connection.send_add_lvap(port.lvap,
                                                      port.block,
                                                      self.downlink.SET_MASK)

        for port in self.uplink.values():
            port.block.radio.connection.send_add_lvap(port.lvap,
                                                      port.block,
                                                      self.uplink.SET_MASK)

    @property
    def ssids(self):
        """Get the ssids assigned to this LVAP."""

        return self._ssids

    @ssids.setter
    def ssids(self, ssids):
        """Set the ssids assigned to this LVAP."""

        self._ssids = ssids

    @property
    def ssid(self):
        """ Get the SSID assigned to this LVAP. """

        return self._ssid

    @ssid.setter
    def ssid(self, ssid):
        """ Set the SSID. """

        if self._ssid == ssid:
            return

        self._ssid = ssid

        for port in self.downlink.values():
            port.block.radio.connection.send_add_lvap(port.lvap,
                                                      port.block,
                                                      self.downlink.SET_MASK)

        for port in self.uplink.values():
            port.block.radio.connection.send_add_lvap(port.lvap,
                                                      port.block,
                                                      self.uplink.SET_MASK)

    @property
    def scheduled_on(self):
        """ Get the default resource block. """

        return self.downlink

    @scheduled_on.setter
    def scheduled_on(self, scheduled_on):
        """Set the default resource block."""

        self.downlink = scheduled_on

    @property
    def downlink(self):
        """ Get the resource blocks assigned to this LVAP in the downlink. """

        return self._downlink

    @downlink.setter
    def downlink(self, downlink):
        """Assign default resource block to LVAP.

        Assign default resource block to LVAP. Accepts as input either a
        ResourcePool or a ResourceBlock. If the resource pool has more than
        one resource block then one random resource block is assigned as both
        downlink and default uplink. The remaining resource blocks are
        assigned as uplink only.

        Args:
            downlink: A ResourcePool or a ResourceBlock
        """

        # Null operation, just return
        if not downlink:
            return

        if isinstance(downlink, ResourcePool):

            pool = downlink

        elif isinstance(downlink, ResourceBlock):

            pool = ResourcePool()
            pool.add(downlink)

        else:

            raise TypeError("Expected ResourcePool, ResourceBlock, got %s",
                            type(downlink))

        current = ResourcePool(list(self.downlink.keys()) +
                               list(self.uplink.keys()))

        # Null operation, just return, but before re-send configuration
        # commands (ports and of tables)
        if current == pool:
            self.set_ports()
            self.clear_tables()
            self.set_tables()
            return

        # clear tables
        self.clear_tables()

        # downlink
        for block in list(self.downlink.keys()):
            del self.downlink[block]

        # uplink
        for block in list(self.uplink.keys()):
            del self.uplink[block]

        # pick default resource block
        default_block = pool.pop()

        # assign default port policy to downlink resource block
        self.downlink[default_block] = RadioPort(self, default_block)

        # set ports
        self.set_ports()

        # set openflow tables
        self.set_tables()

        # assign remaining blocks (if any) to the uplink
        for block in pool:
            self.uplink[block] = RadioPort(self, block)

    @property
    def uplink(self):
        """ Get the resource blocks assigned to this LVAP in the uplink. """

        return self._uplink

    @property
    def port(self):
        """Return the port on which this LVAP is scheduled. """

        if not self.scheduled_on:
            return None

        default_port = next(iter(self.scheduled_on.values()))
        return default_port

    @port.setter
    def port(self, value):
        """Set the Port."""

        if not self.scheduled_on:
            return None

        default_block = next(iter(self.scheduled_on.values()))
        self.scheduled_on[default_block] = value

    @property
    def wtp(self):
        """Return the wtp on which this LVAP is scheduled on."""

        if not self.scheduled_on:
            return None

        default_block = next(iter(self.scheduled_on.keys()))
        return default_block.radio

    @wtp.setter
    def wtp(self, wtp):
        """Assigns LVAP to new wtp."""

        default_block = next(iter(self.scheduled_on.keys()))
        matching = wtp.supports & ResourcePool([default_block])
        self.scheduled_on = matching.pop() if matching else None

    def to_dict(self):
        """ Return a JSON-serializable dictionary representing the LVAP """

        return {'addr': self.addr,
                'bssid': self.bssid,
                'port': self.port,
                'ports': self.ports,
                'wtp': self.wtp,
                'scheduled_on': [k for k in self.downlink.keys()],
                'downlink': [k for k in self.downlink.keys()],
                'uplink': [k for k in self.uplink.keys()],
                'ssids': self.ssids,
                'assoc_id': self.assoc_id,
                'ssid': self.ssid,
                'encap': self.encap,
                'supports': self.supports,
                'tx_samples': self.tx_samples,
                'rx_samples': self.rx_samples,
                'rates': self.rates,
                'authentication_state': self.authentication_state,
                'association_state': self.association_state}

    def __str__(self):

        accum = []
        accum.append("addr ")
        accum.append(str(self.addr))
        accum.append(" bssid ")
        accum.append(str(self.bssid))
        accum.append(" ssid ")
        accum.append(str(self.ssid))

        if self.ssids:

            accum.append(" ssids [ ")
            accum.append(str(self.ssids[0]))

            for ssid in self.ssids[1:]:
                accum.append(", ")
                accum.append(str(ssid))

            accum.append(" ]")

        accum.append(" assoc_id ")
        accum.append(str(self.assoc_id))

        if self.association_state:
            accum.append(" ASSOC")

        if self.authentication_state:
            accum.append(" AUTH")

        return ''.join(accum)

    def __hash__(self):
        return hash(self.addr)

    def __eq__(self, other):
        if isinstance(other, LVAP):
            return self.addr == other.addr
        return False

    def __ne__(self, other):
        return not self.__eq__(other)
