#!/usr/bin/env python3
"""DNS/DHCP wire probes; only loopback/LAN fixtures use the synthetic upstream."""
import argparse
import ipaddress
import os
import socket
import struct
import time


def question(name):
    return b"".join(bytes([len(part)]) + part.encode() for part in name.split(".")) + b"\0\0\1\0\1"


def query(server, port, name):
    identifier = int.from_bytes(os.urandom(2), "big")
    packet = struct.pack("!6H", identifier, 0x100, 1, 0, 0, 0) + question(name)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        sock.sendto(packet, (server, port))
        answer, _ = sock.recvfrom(4096)
    ident, flags, _, count, _, _ = struct.unpack_from("!6H", answer)
    if ident != identifier or not flags & 0x8000 or flags & 15 or not count:
        raise ValueError("DNS response missing a successful answer: " + name)
    return answer


def upstream():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 5053))
        while True:
            packet, peer = sock.recvfrom(4096)
            offset = 12
            while packet[offset]:
                offset += packet[offset] + 1
            end = offset + 5
            answer = (struct.pack("!6H", struct.unpack_from("!H", packet)[0], 0x8180, 1, 1, 0, 0)
                      + packet[12:end] + b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4)
                      + socket.inet_aton("192.0.2.123"))
            sock.sendto(answer, peer)


def options(packet):
    result = {}
    index = 240
    while index < len(packet):
        code = packet[index]
        index += 1
        if code == 255:
            break
        if code == 0:
            continue
        size = packet[index]
        index += 1
        result[code] = packet[index:index + size]
        index += size
    return result


def dhcp(interface):
    transaction = 0x52325331
    mac = bytes.fromhex("020000000123")
    fixed = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 1, 1, 6, 0, transaction, 0, 0x8000,
                        b"\0" * 4, b"\0" * 4, b"\0" * 4, b"\0" * 4, mac + b"\0" * 10,
                        b"\0" * 64, b"\0" * 128) + bytes.fromhex("63825363")
    # A DHCP client has no IPv4 address yet. Receive at L2 like dhclient,
    # otherwise Linux may discard the offer before an AF_INET socket sees it.
    with socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(0x0800)) as incoming, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        incoming.bind((interface, 0))
        incoming.settimeout(5)
        def receive(kind):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                packet = incoming.recv(4096)
                ihl = (packet[0] & 15) * 4
                if len(packet) < ihl + 8 + 240 or packet[9] != 17:
                    continue
                if struct.unpack_from("!H", packet, ihl + 2)[0] != 68:
                    continue
                message = packet[ihl + 8:]
                if struct.unpack_from("!I", message, 4)[0] == transaction and options(message).get(53) == kind:
                    return message
            raise ValueError("Matching DHCP response was not received")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
        sock.bind(("0.0.0.0", 68))
        sock.settimeout(5)
        sock.sendto(fixed + b"\x35\x01\x01\x37\x03\x01\x03\x06\xff", ("255.255.255.255", 67))
        offer = receive(b"\x02")
        opts = options(offer)
        if struct.unpack_from("!I", offer, 4)[0] != transaction or opts.get(53) != b"\x02":
            raise ValueError("Expected matching DHCP offer")
        address, server = offer[16:20], opts[54]
        lease = ipaddress.ip_address(address)
        if not ipaddress.ip_address("10.0.0.100") <= lease <= ipaddress.ip_address("10.0.0.199"):
            raise ValueError("Lease outside the declared pool")
        sock.sendto(fixed + b"\x35\x01\x03\x32\x04" + address + b"\x36\x04" + server + b"\xff",
                    ("255.255.255.255", 67))
        ack = receive(b"\x05")
        opts = options(ack)
        if opts.get(53) != b"\x05" or opts.get(3) != socket.inet_aton("10.0.0.1") or opts.get(6) != socket.inet_aton("10.0.0.1"):
            raise ValueError("DHCP ACK has incorrect router/DNS options")
        print(str(lease))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("upstream", "dhcp", "dns", "doh"))
    parser.add_argument("--interface", default="r2s-client")
    args = parser.parse_args()
    if args.action == "upstream":
        upstream()
    elif args.action == "dhcp":
        dhcp(args.interface)
    elif args.action == "dns":
        for name, expected in (("smoke.home.arpa", "10.0.0.1"), ("example.com", "192.0.2.123"),
                               ("blocked.example", "0.0.0.0")):
            answer = query("10.0.0.1", 53, name)
            if socket.inet_aton(expected) not in answer:
                raise ValueError("Incorrect DNS result: " + name)
        print("LAN/internal/upstream/blocklist DNS probes passed")
    else:
        last = None
        for _ in range(18):
            try:
                query("127.0.0.1", 5053, "example.com")
                print("Unprivileged image dnscrypt-proxy returned a real DoH DNS answer")
                return
            except (OSError, ValueError) as error:
                last = error
                time.sleep(2)
        raise ValueError("DoH resolution did not become available: " + str(last))


if __name__ == "__main__":
    main()
