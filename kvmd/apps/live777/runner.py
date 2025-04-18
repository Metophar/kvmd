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
import asyncio.subprocess
import socket
import dataclasses

import netifaces

from ... import tools
from ... import aiotools
from ... import aioproc

from ...logging import get_logger

from .stun import StunNatType
from .stun import Stun


# =====
@dataclasses.dataclass(frozen=True)
class _Netcfg:
    nat_type:  StunNatType = dataclasses.field(default=StunNatType.ERROR)
    src_ip:    str = dataclasses.field(default="")
    ext_ip:    str = dataclasses.field(default="")
    stun_host: str = dataclasses.field(default="")
    stun_ip:   str = dataclasses.field(default="")
    stun_port: int = dataclasses.field(default=0)


# =====
class Live777Runner:
    def __init__(  # pylint: disable=too-many-arguments
        self,
        stun_host: str,
        stun_port: int,
        stun_timeout: float,
        stun_retries: int,
        stun_retries_delay: float,

        check_interval: int,
        check_retries: int,
        check_retries_delay: float,

        cmd: list[str],
        cmd_remove: list[str],
        cmd_append: list[str],
    ) -> None:

        self.__stun = Stun(stun_host, stun_port, stun_timeout, stun_retries, stun_retries_delay)

        self.__check_interval = check_interval
        self.__check_retries = check_retries
        self.__check_retries_delay = check_retries_delay

        self.__cmd = tools.build_cmd(cmd, cmd_remove, cmd_append)

        self.__live777_task: (asyncio.Task | None) = None
        self.__live777_proc: (asyncio.subprocess.Process | None) = None

    def run(self) -> None:
        logger = get_logger(0)
        logger.info("Starting Live777 Runner ...")
        aiotools.run(self.__run(), self.__stop_live777())
        logger.info("Bye-bye")

    # =====

    async def __run(self) -> None:
        logger = get_logger(0)
        logger.info("Probing the network first time ...")

        prev_netcfg: (_Netcfg | None) = None
        while True:
            retry = 0
            netcfg = _Netcfg()
            for retry in range(1 if prev_netcfg is None else self.__check_retries):
                netcfg = await self.__get_netcfg()
                if netcfg.ext_ip:
                    break
                await asyncio.sleep(self.__check_retries_delay)
            if retry != 0 and netcfg.ext_ip:
                logger.info("I'm fine, continue working ...")

            if netcfg != prev_netcfg:
                logger.info("Got new %s", netcfg)
                if netcfg.src_ip:
                    await self.__stop_live777()
                    await self.__start_live777(netcfg)
                else:
                    logger.error("Empty src_ip; stopping Live777 ...")
                    await self.__stop_live777()
                prev_netcfg = netcfg

            await asyncio.sleep(self.__check_interval)

    async def __get_netcfg(self) -> _Netcfg:
        src_ip = (self.__get_default_ip() or "0.0.0.0")
        info = await self.__stun.get_info(src_ip, 0)
        return _Netcfg(**dataclasses.asdict(info))

    def __get_default_ip(self) -> str:
        try:
            gws = netifaces.gateways()
            if "default" in gws:
                for proto in [socket.AF_INET, socket.AF_INET6]:
                    if proto in gws["default"]:
                        iface = gws["default"][proto][1]
                        addrs = netifaces.ifaddresses(iface)
                        return addrs[proto][0]["addr"]

            for iface in netifaces.interfaces():
                if not iface.startswith(("lo", "docker")):
                    addrs = netifaces.ifaddresses(iface)
                    for proto in [socket.AF_INET, socket.AF_INET6]:
                        if proto in addrs:
                            return addrs[proto][0]["addr"]
        except Exception as ex:
            get_logger().error("Can't get default IP: %s", tools.efmt(ex))
        return ""

    # =====

    @aiotools.atomic_fg
    async def __start_live777(self, netcfg: _Netcfg) -> None:
        get_logger(0).info("Starting Live777 ...")
        assert not self.__live777_task
        self.__live777_task = asyncio.create_task(self.__live777_task_loop(netcfg))

    @aiotools.atomic_fg
    async def __stop_live777(self) -> None:
        if self.__live777_task:
            get_logger(0).info("Stopping Live777 ...")
            self.__live777_task.cancel()
            await asyncio.gather(self.__live777_task, return_exceptions=True)
        await self.__kill_live777_proc()
        self.__live777_task = None

    # =====

    async def __live777_task_loop(self, netcfg: _Netcfg) -> None:
        logger = get_logger(0)
        while True:
            try:
                await self.__start_live777_proc(netcfg)
                assert self.__live777_proc is not None
                await aioproc.log_stdout_infinite(self.__live777_proc, logger)
                raise RuntimeError("Live777 unexpectedly died")
            except asyncio.CancelledError:
                break
            except Exception:
                if self.__live777_proc:
                    logger.exception("Unexpected Live777 error: pid=%d", self.__live777_proc.pid)
                else:
                    logger.exception("Can't start Live777")
                await self.__kill_live777_proc()
                await asyncio.sleep(1)

    async def __start_live777_proc(self, netcfg: _Netcfg) -> None:
        assert self.__live777_proc is None
        placeholders = {
            "o_stun_server": f"--stun-server={netcfg.stun_ip}:{netcfg.stun_port}",
            **{
                key: str(value)
                for (key, value) in dataclasses.asdict(netcfg).items()
            },
        }
        cmd = list(self.__cmd)
        if not netcfg.ext_ip:
            placeholders["o_stun_server"] = ""
            while "{o_stun_server}" in cmd:
                cmd.remove("{o_stun_server}")
        cmd = [
            part.format(**placeholders)
            for part in cmd
        ]
        self.__live777_proc = await aioproc.run_process(
            cmd=cmd,
            env={
                "LIVE777_STUN_URL": f"stun:{netcfg.stun_host}:{netcfg.stun_port}",
                "LIVE777_VIDEO_SOURCE": "kvmd::ustreamer::h264",
                "LIVE777_AUDIO_SOURCE": "hw:tc358743,0",
            },
        )
        get_logger(0).info("Started Live777 pid=%d: %s", self.__live777_proc.pid, tools.cmdfmt(cmd))

    async def __kill_live777_proc(self) -> None:
        if self.__live777_proc:
            await aioproc.kill_process(self.__live777_proc, 5, get_logger(0))
        self.__live777_proc = None 