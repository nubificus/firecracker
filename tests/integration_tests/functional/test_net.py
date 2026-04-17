# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the net device."""

import re
import time

import pytest

import host_tools.network as net_tools  # pylint: disable=import-error
from framework import utils

# The iperf version to run this tests with
IPERF_BINARY = "iperf3"


def test_high_ingress_traffic(uvm_plain_any):
    """
    Run iperf rx with high UDP traffic.
    """
    test_microvm = uvm_plain_any
    test_microvm.spawn()
    test_microvm.basic_config()

    # Create tap before configuring interface.
    test_microvm.add_net_iface()
    tap = test_microvm.iface["eth0"]["tap"]
    guest_ip = test_microvm.iface["eth0"]["iface"].guest_ip
    # Set the tap's tx queue len to 5. This increases the probability
    # of filling the tap under high ingress traffic.
    tap.set_tx_queue_len(5)

    # Start the microvm.
    test_microvm.start()

    # Start iperf3 server on the guest.
    test_microvm.ssh.run("{} -sD\n".format(IPERF_BINARY))
    time.sleep(1)

    # Start iperf3 client on the host. Send 1Gbps UDP traffic.
    # If the net device breaks, iperf will freeze. We have to use a timeout.
    utils.run_cmd(
        "timeout 30 {} {} -c {} -u -V -b 1000000000 -t 30".format(
            test_microvm.netns.cmd_prefix(),
            IPERF_BINARY,
            guest_ip,
        ),
        ignore_return_code=True,
    )

    # Check if the high ingress traffic broke the net interface.
    # If the net interface still works we should be able to execute
    # ssh commands.
    exit_code, _, _ = test_microvm.ssh.run("echo success\n")
    assert exit_code == 0


def test_multi_queue_unsupported(uvm_plain):
    """
    Creates multi-queue tap device and tries to add it to firecracker.
    """
    microvm = uvm_plain
    microvm.spawn()
    microvm.basic_config()

    tapname = microvm.id[:8] + "tap1"

    utils.run_cmd(f"ip tuntap add name {tapname} mode tap multi_queue")
    utils.run_cmd(f"ip link set {tapname} netns {microvm.netns.id}")

    expected_msg = re.escape(
        "Could not create the network device: Open tap device failed:"
        " Error while creating ifreq structure: Invalid argument (os error 22)."
        f" Invalid TUN/TAP Backend provided by {tapname}. Check our documentation on setting"
        " up the network devices."
    )

    with pytest.raises(RuntimeError, match=expected_msg):
        microvm.api.network.put(
            iface_id="eth0",
            host_dev_name=tapname,
            guest_mac="AA:FC:00:00:00:01",
        )


def test_tap_mtu_advertised_to_guest(uvm_plain_any):
    """
    Verify that VIRTIO_NET_F_MTU correctly advertises the TAP MTU to the guest.

    Configures multiple TAP interfaces with distinct MTU values and checks that
    each guest network interface reports the MTU matching its host TAP.
    """
    vm = uvm_plain_any
    vm.spawn()
    vm.basic_config()

    # (interface index, MTU) pairs with varied values
    iface_mtus = [(0, 1500), (1, 1400), (2, 3500)]

    for idx, mtu in iface_mtus:
        iface = net_tools.NetIfaceConfig.with_id(idx)
        # Create the tap without registering with Firecracker yet, so we can
        # set the MTU before Firecracker reads it (MTU is read at device init).
        vm.add_net_iface(iface, api=False)
        vm.netns.check_output(f"ip link set {iface.tap_name} mtu {mtu}")
        # Register with Firecracker; tap.mtu() is called during this API call.
        vm.api.network.put(
            iface_id=iface.dev_name,
            host_dev_name=iface.tap_name,
            guest_mac=iface.guest_mac,
        )

    vm.start()

    # Verify each guest interface carries the expected MTU.
    # SSH runs over eth0; we can still query other interfaces from there.
    for idx, mtu in iface_mtus:
        iface_name = f"eth{idx}"
        guest_ip = vm.iface[iface_name]["iface"].guest_ip
        guest_if = net_tools.get_guest_net_if_name(vm.ssh, guest_ip)
        assert (
            guest_if is not None
        ), f"Could not find guest interface for {iface_name} ({guest_ip})"
        _, stdout, _ = vm.ssh.check_output(f"cat /sys/class/net/{guest_if}/mtu")
        assert (
            int(stdout.strip()) == mtu
        ), f"{iface_name} (guest: {guest_if}): expected MTU {mtu}, got {stdout.strip()}"
