#!/usr/bin/env python3
# Copyright (c) 2020, ZIH, Technische Universitaet Dresden, Federal Republic of Germany
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright notice,
#       this list of conditions and the following disclaimer in the documentation
#       and/or other materials provided with the distribution.
#     * Neither the name of metricq nor the names of its contributors
#       may be used to endorse or promote products derived from this software
#       without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import asyncio
import datetime
from enum import Enum
from enum import auto as enum_auto
from typing import Any, Dict, Optional, Set

import aio_pika
import click
import click_completion
import click_log
import humanize
import metricq
from dateutil.parser import isoparse as parse_iso_datetime
from dateutil.tz import tzlocal
from metricq.types import Timedelta

from .logging import get_root_logger
from .utils import ChoiceParam, CommandLineChoice, DurationParam, metricq_server_option
from .version import version as client_version

logger = get_root_logger()

click_completion.init()


class IgnoredEvent(CommandLineChoice, Enum):
    ErrorResponses = enum_auto()

    @classmethod
    def default(cls):
        return cls.ErrorResponses


IGNORED_EVENT = ChoiceParam(IgnoredEvent, "ignored")

TIMEOUT = DurationParam(default=None)


class DiscoverErrorResponse(ValueError):
    pass


class DiscoverResponse:
    def __init__(
        self,
        alive: bool = True,
        current_time: Optional[str] = None,
        starting_time: Optional[str] = None,
        uptime: Optional[int] = None,
        metricq_version: Optional[str] = None,
        python_version: Optional[str] = None,
        client_version: Optional[str] = None,
        hostname: Optional[str] = None,
    ):
        self.alive = alive
        self.metricq_version = metricq_version
        self.python_version = python_version
        self.client_version = client_version
        self.hostname = hostname

        self.current_time = self._parse_datetime(current_time)
        self.starting_time = self._parse_datetime(starting_time)
        self.uptime: Optional[datetime.timedelta] = None

        try:
            if uptime is None:
                if self.current_time is not None and self.starting_time is not None:
                    self.uptime = self.current_time - self.starting_time
            else:
                self.uptime = (
                    datetime.timedelta(seconds=uptime)
                    if uptime < 1e9
                    else datetime.timedelta(microseconds=int(uptime // 1e3))
                )
        except (ValueError, TypeError):
            pass

    @staticmethod
    def parse(response: Dict[str, Any]) -> "DiscoverResponse":
        error = response.get("error")
        if error is not None:
            raise DiscoverErrorResponse(error)

        return DiscoverResponse(
            alive=bool(response.get("alive")),
            starting_time=response.get("startingTime"),
            current_time=response.get("currentTime"),
            uptime=response.get("uptime"),
            metricq_version=response.get("metricqVersion"),
            client_version=response.get("version"),
            python_version=response.get("pythonVersion"),
            hostname=response.get("hostname"),
        )

    @classmethod
    def _parse_datetime(cls, iso_string) -> Optional[datetime.datetime]:
        if iso_string is None:
            return None
        else:
            try:
                dt = parse_iso_datetime(iso_string)
                return dt.astimezone(tzlocal()).replace(tzinfo=None)
            except (AttributeError, ValueError, TypeError, OverflowError) as e:
                logger.warning("Failed to parse ISO datestring ({}): {}", iso_string, e)
                return None

    def _fmt_parts(self):
        unknown_color = "bright_white"

        alive = "alive" if self.alive else click.style("dead", fg="bright_red")
        yield f"currently {alive},"

        try:
            yield f"up for {humanize.naturaldelta(self.uptime)}"
        except Exception:
            yield click.style("unknown uptime", fg=unknown_color)

        try:
            yield f"(started {humanize.naturalday(self.starting_time)})"
        except Exception as e:
            logger.warning(
                "Failed to convert {} to naturaltime: {}", self.starting_time, e
            )

        if self.client_version:
            yield f"version {self.client_version}"

        if self.python_version:
            yield f"(python {self.python_version})"

        if self.metricq_version:
            yield f"running {self.metricq_version}"

        if self.hostname:
            yield f"on {self.hostname}"

    def __str__(self):
        return " ".join(self._fmt_parts())


class Status(Enum):
    Ok = enum_auto()
    Warning = enum_auto()
    Error = enum_auto()


def echo_status(status: Status, token: str, msg: str):
    style_status = {
        Status.Ok: {"text": "✔️", "fg": "green"},
        Status.Warning: {"text": "⚠", "fg": "yellow"},
        Status.Error: {"text": "❌", "fg": "red"},
    }

    status_prefix = click.style(**style_status[status])  # type: ignore

    click.echo(f'{status_prefix} {click.style(token, fg="cyan")}: {msg}')


class MetricQDiscover(metricq.Agent):
    def __init__(self, server, timeout: Timedelta, ignore_events: Set[IgnoredEvent]):
        super().__init__("discover", server, add_uuid=True)
        self.timeout = timeout
        self.ignore_events: Set[IgnoredEvent] = ignore_events

    async def discover(self):
        await self.connect()
        await self.rpc_consume()

        self._management_broadcast_exchange = (
            await self._management_channel.declare_exchange(
                name=self._management_broadcast_exchange_name,
                type=aio_pika.ExchangeType.FANOUT,
                durable=True,
            )
        )

        await self.rpc(
            self._management_broadcast_exchange,
            "discover",
            response_callback=self.on_discover,
            function="discover",
            cleanup_on_response=False,
        )

        await asyncio.sleep(self.timeout.s)

    def on_discover(self, from_token, **response):
        logger.debug("response: {}", response)
        try:
            self.pretty_print(
                from_token,
                response=DiscoverResponse.parse(response),
            )
        except DiscoverErrorResponse as error:
            if IgnoredEvent.ErrorResponses in self.ignore_events:
                logger.debug(f"Ignored error response from {from_token}: {error}")
                return

            error_msg = click.style(str(error), fg="bright_red")
            echo_status(
                Status.Error,
                from_token,
                f"response indicated an error: {error_msg}",
            )

    def pretty_print(self, from_token, response: DiscoverResponse):
        status = Status.Ok if response.alive else Status.Warning

        echo_status(status, from_token, str(response))


@click.command()
@click.version_option(version=client_version)
@metricq_server_option()
@click.option(
    "-t",
    "--timeout",
    type=TIMEOUT,
    default=TIMEOUT.default,
    help="Wait at most this long for replies.",
)
@click.option("--ignore", type=IGNORED_EVENT, multiple=True, help="Messages to ignore.")
@click_log.simple_verbosity_option(logger, default="warning")
def main(server, timeout: Timedelta, ignore):
    """Send a RPC broadcast on the MetricQ network and wait for replies from online clients."""
    d = MetricQDiscover(
        server,
        timeout=timeout,
        ignore_events=set(event for event in ignore),
    )

    asyncio.run(d.discover())


if __name__ == "__main__":
    main()
