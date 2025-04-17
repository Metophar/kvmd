# ========================================================================== #
#                                                                            #
#    KVMD - The main PiKVM daemon.                                           #
#                                                                            #
#    Copyright (C) 2018-2024  Maxim Devaev <mdevaev@gmail.com>               #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
# ========================================================================== #


import asyncio
import socket
import ipaddress
import struct
import secrets
import dataclasses
import enum

from ... import tools
from ... import aiotools

from ...logging import get_logger


# =====
class StunNatType(enum.Enum):
    ERROR               = ""
    BLOCKED             = "Blocked"
    OPEN_INTERNET       = "Open Internet"
    SYMMETRIC_UDP_FW    = "Symmetric UDP Firewall"
    FULL_CONE_NAT       = "Full Cone NAT"
    RESTRICTED_NAT      = "Restricted NAT"
    RESTRICTED_PORT_NAT = "Restricted Port NAT"
    SYMMETRIC_NAT       = "Symmetric NAT"
    CHANGED_ADDR_ERROR  = "Error when testing on Changed-IP and Port"


@dataclasses.dataclass(frozen=True)
class StunInfo:
    nat_type:  StunNatType
    src_ip:    str
    ext_ip:    str
    stun_host: str
    stun_ip:   str
    stun_port: int


@dataclasses.dataclass(frozen=True)
class _StunAddress:
    ip:   str
    port: int


@dataclasses.dataclass(frozen=True)
class _StunResponse:
    ok:      bool
    ext:     (_StunAddress | None) = dataclasses.field(default=None)
    src:     (_StunAddress | None) = dataclasses.field(default=None)
    changed: (_StunAddress | None) = dataclasses.field(default=None)


# =====
class Stun:
    def __init__(
        self,
        host: str,
        port: int,
        timeout: float,
        retries: int,
        retries_delay: float,
    ) -> None:

        self.__host = host
        self.__port = port
        self.__timeout = timeout
        self.__retries = retries
        self.__retries_delay = retries_delay

        self.__stun_ip = ""
        self.__sock: (socket.socket | None) = None

    async def get_info(self, src_ip: str, src_port: int) -> StunInfo:
        nat_type = StunNatType.ERROR
        ext_ip = ""
        try:
            (src_fam, _, _, _, src_addr) = (await self.__retried_getaddrinfo_udp(src_ip, src_port))[0]

            stun_ips = [
                stun_addr[0]
                for (stun_fam, _, _, _, stun_addr) in (await self.__retried_getaddrinfo_udp(self.__host, self.__port))
                if stun_fam == src_fam
            ]
            if not stun_ips:
                raise RuntimeError(f"Can't resolve {src_fam.name} address for STUN")
            if not self.__stun_ip or self.__stun_ip not in stun_ips:
                self.__stun_ip = stun_ips[0]

            with socket.socket(src_fam, socket.SOCK_DGRAM) as self.__sock:
                self.__sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.__sock.settimeout(self.__timeout)
                self.__sock.bind(src_addr)
                (nat_type, resp) = await self.__get_nat_type(src_ip)
                ext_ip = (resp.ext.ip if resp.ext is not None else "")
        except Exception as ex:
            get_logger(0).error("Can't get STUN info: %s", tools.efmt(ex))
        finally:
            self.__sock = None

        return StunInfo(
            nat_type=nat_type,
            src_ip=src_ip,
            ext_ip=ext_ip,
            stun_host=self.__host,
            stun_ip=self.__stun_ip,
            stun_port=self.__port,
        )

    async def __retried_getaddrinfo_udp(self, host: str, port: int) -> list:
        retries = self.__retries
        while True:
            try:
                return socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
            except Exception:
                retries -= 1
                if retries == 0:
                    raise
            await asyncio.sleep(self.__retries_delay)

    async def __get_nat_type(self, src_ip: str) -> tuple[StunNatType, _StunResponse]:
        first = await self.__make_request("First probe", self.__stun_ip, b"")
        if not first.ok:
            return (StunNatType.BLOCKED, first)

        req = struct.pack(">HHI", 0x0003, 0x0004, 0x00000006)  # Change-Request
        resp = await self.__make_request("Change request [ext_ip == src_ip]", self.__stun_ip, req)

        if first.ext is not None and first.ext.ip == src_ip:
            if resp.ok:
                return (StunNatType.OPEN_INTERNET, resp)
            return (StunNatType.SYMMETRIC_UDP_FW, resp)

        if resp.ok:
            return (StunNatType.FULL_CONE_NAT, resp)

        if first.changed is None:
            raise RuntimeError(f"Changed addr is None: {first}")
        resp = await self.__make_request("Change request [ext_ip != src_ip]", first.changed, b"")
        if not resp.ok:
            return (StunNatType.CHANGED_ADDR_ERROR, resp)

        if resp.ext == first.ext:
            req = struct.pack(">HHI", 0x0003, 0x0004, 0x00000002)
            resp = await self.__make_request("Change port", first.changed.ip, req)
            if resp.ok:
                return (StunNatType.RESTRICTED_NAT, resp)
            return (StunNatType.RESTRICTED_PORT_NAT, resp)

        return (StunNatType.SYMMETRIC_NAT, resp)

    async def __make_request(self, ctx: str, addr: (_StunAddress | str), req: bytes) -> _StunResponse:
        if isinstance(addr, _StunAddress):
            addr_t = (addr.ip, addr.port)
        else:  # str
            addr_t = (addr, self.__port)

        trans_id = b"\x21\x12\xA4\x42" + secrets.token_bytes(12)
        (resp, error) = (b"", "")
        for _ in range(self.__retries):
            (resp, error) = await self.__inner_make_request(trans_id, req, addr_t)
            if not error:
                break
            await asyncio.sleep(self.__retries_delay)
        if error:
            get_logger(0).error("%s: Can't perform STUN request after %d retries; last error: %s",
                                ctx, self.__retries, error)
            return _StunResponse(ok=False)

        parsed: dict[str, _StunAddress] = {}
        offset = 0
        remaining = len(resp)
        while remaining > 0:
            (attr_type, attr_len) = struct.unpack(">HH", resp[offset : offset + 4])  # noqa: E203
            offset += 4
            field = {
                0x0001: "ext",      # MAPPED-ADDRESS
                0x0020: "ext",      # XOR-MAPPED-ADDRESS
                0x0004: "src",      # SOURCE-ADDRESS
                0x0005: "changed",  # CHANGED-ADDRESS
            }.get(attr_type)
            if field is not None:
                parsed[field] = self.__parse_address(resp[offset:], (trans_id if attr_type == 0x0020 else b""))
            offset += attr_len
            remaining -= (4 + attr_len)
        return _StunResponse(ok=True, **parsed)

    async def __inner_make_request(self, trans_id: bytes, req: bytes, addr: tuple[str, int]) -> tuple[bytes, str]:
        assert self.__sock is not None

        msg = struct.pack(">HHH", 0x0001, len(req), 0x2112) + trans_id + req
        try:
            self.__sock.sendto(msg, addr)
            (data, _) = self.__sock.recvfrom(2048)
            if len(data) < 20:
                raise RuntimeError("Response is too short")
            if data[0:2] != b"\x01\x01":
                raise RuntimeError("Invalid response type")
            if data[4:20] != trans_id:
                raise RuntimeError("Transaction ID mismatch")
            return (data[20:], "")
        except Exception as err:
            return (b"", str(err))

    def __parse_address(self, data: bytes, trans_id: bytes) -> _StunAddress:
        if len(data) < 4:
            raise RuntimeError("Address: data is too short")
        if data[1] not in [0x01, 0x02]:  # IPv4 or IPv6
            raise RuntimeError(f"Invalid address family: {data[1]}")
        (port,) = struct.unpack(">H", self.__trans_xor(data[2:4], trans_id))
        if data[1] == 0x01:  # IPv4
            if len(data) < 8:
                raise RuntimeError("IPv4 address: data is too short")
            ip = str(ipaddress.IPv4Address(int.from_bytes(self.__trans_xor(data[4:8], trans_id), "big")))
        else:  # IPv6
            if len(data) < 20:
                raise RuntimeError("IPv6 address: data is too short")
            ip = str(ipaddress.IPv6Address(int.from_bytes(self.__trans_xor(data[4:20], trans_id), "big")))
        return _StunAddress(ip=ip, port=port)

    def __trans_xor(self, data: bytes, trans_id: bytes) -> bytes:
        if not trans_id:
            return data
        magic = b"\x21\x12\xA4\x42"
        xor = (magic + trans_id) * (len(data) // len(magic + trans_id) + 1)
        return bytes(a ^ b for (a, b) in zip(data, xor[:len(data)])) 