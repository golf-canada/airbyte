"""
MIT License

Copyright (c) 2020 Airbyte

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


from abc import ABC, abstractmethod
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Tuple

import pendulum
import requests
from airbyte_protocol import SyncMode
from base_python.logger import AirbyteLogger
from base_python.sdk.abstract_source import AbstractSource
from base_python.sdk.streams.auth.token import TokenAuthenticator
from base_python.sdk.streams.core import Stream
from base_python.sdk.streams.http import HttpStream
from pendulum import DateTime
from slack_sdk import WebClient


class SlackStream(HttpStream, ABC):
    url_base = "https://slack.com/api/"

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        # Slack uses a cursor-based pagination strategy.
        # Extract the cursor from the response if it exists and return it in a format that can be used to update request parameters
        json_response = response.json()
        next_cursor = json_response.get("response_metadata", {}).get("next_cursor")
        if next_cursor:
            return {"cursor": json_response.get("response_metadata", {}).get("next_cursor")}

    def request_params(
        self,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> MutableMapping[str, Any]:
        params = {"limit": 100}
        if next_page_token:
            params.update(**next_page_token)
        return params

    def parse_response(
        self,
        response: requests.Response,
        stream_state: Mapping[str, Any] = None,
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> Iterable[Mapping]:
        json_response = response.json()
        for record in json_response.get(self.data_field, []):
            yield record

    def backoff_time(self, response: requests.Response) -> Optional[float]:
        # This method is called if we run into the rate limit. Slack puts the retry time in the `Retry-After` response header so we
        # we return that value. If the response is anything other than a 429 (e.g: 5XX) fall back on default retry behavior.
        # https://api.slack.com/docs/rate-limits#web
        if response.status_code == 429:
            return int(response.headers.get("Retry-After", 0))

    @property
    @abstractmethod
    def data_field(self) -> str:
        """The name of the field in the response which contains the data"""


class Channels(SlackStream):
    data_field = "channels"

    def path(self, **kwargs) -> str:
        return "conversations.list"

    def request_params(self, **kwargs) -> MutableMapping[str, Any]:
        p = super().request_params(**kwargs)
        p["types"] = "public_channel"
        return p


class ChannelMembers(SlackStream):
    data_field = "members"

    def path(self, **kwargs) -> str:
        return "conversations.members"

    def request_params(self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, Any] = None, **kwargs) -> MutableMapping[str, Any]:
        params = super().request_params(stream_state=stream_state, stream_slice=stream_slice, **kwargs)
        params["channel"] = stream_slice["channel_id"]
        return params

    def parse_response(self, response: requests.Response, stream_slice: Mapping[str, Any] = None, **kwargs) -> Iterable[Mapping]:
        for member_id in super().parse_response(response, **kwargs):
            # Slack just returns raw IDs as a string, so we want to put them in a "join table" format
            yield {"member_id": member_id, "channel_id": stream_slice["channel_id"]}

    def stream_slices(self, **kwargs) -> Iterable[Optional[Mapping[str, any]]]:
        channels_stream = Channels(authenticator=self.authenticator)
        for channel_record in channels_stream.read_records(sync_mode=SyncMode.full_refresh):
            yield {"channel_id": channel_record["id"]}


class Users(SlackStream):
    data_field = "members"

    def path(self, **kwargs) -> str:
        return "users.list"


# Incremental Streams
def chunk_date_range(start_date: DateTime, interval=1) -> Iterable[Mapping[str, any]]:
    """
    Returns a list of the beginning and ending timetsamps of each day between the start date and now.
    The return value is a list of dicts {'oldest': float, 'latest': float} which can be used directly with the Slack API
    """
    intervals = []
    now = pendulum.now()
    # Each stream_slice contains the beginning and ending timestamp for a 24 hour period
    while start_date <= now:
        end = start_date.add(days=interval)
        intervals.append({"oldest": start_date.timestamp(), "latest": end.timestamp()})
        start_date = start_date.add(days=1)

    return intervals


class IncrementalMessageStream(SlackStream, ABC):
    data_field = "messages"
    cursor_field = "ts"

    def __init__(self, default_start_date: DateTime, **kwargs):
        self._default_start_date = default_start_date
        super().__init__(**kwargs)

    def request_params(self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, Any] = None, **kwargs) -> MutableMapping[str, Any]:
        params = super().request_params(stream_state=stream_state, stream_slice=stream_slice, **kwargs)
        params.update(**stream_slice)
        return params

    def get_updated_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]):
        current_stream_state = current_stream_state or {}
        current_stream_state["ts"] = max(float(latest_record["ts"]), current_stream_state.get("ts", 0))
        return current_stream_state


class ChannelMessages(IncrementalMessageStream):
    def path(self, **kwargs) -> str:
        return "conversations.history"

    def stream_slices(self, stream_state: Mapping[str, Any] = None, **kwargs) -> Iterable[Optional[Mapping[str, any]]]:
        stream_state = stream_state or {}
        start_date = pendulum.from_timestamp(stream_state.get("start_ts", self._default_start_date.timestamp()))
        return chunk_date_range(start_date)

    def read_records(self, stream_slice: Optional[Mapping[str, Any]] = None, **kwargs) -> Iterable[Mapping[str, Any]]:
        # Channel is provided when reading threads
        if "channel" in stream_slice:
            yield from super().read_records(stream_slice=stream_slice, **kwargs)
        else:
            # if channel is not provided, then get channels and read accordingly
            channels = Channels(authenticator=self.authenticator)
            for channel_record in channels.read_records(sync_mode=SyncMode.full_refresh):
                stream_slice["channel"] = channel_record["id"]
                yield from super().read_records(stream_slice=stream_slice, **kwargs)


class Threads(IncrementalMessageStream):
    def __init__(self, lookback_window: Mapping[str, int], **kwargs):
        self.messages_lookback_window = lookback_window
        super().__init__(**kwargs)

    def path(self, **kwargs) -> str:
        return "conversations.replies"

    def stream_slices(self, stream_state: Mapping[str, Any] = None, **kwargs) -> Iterable[Optional[Mapping[str, any]]]:
        """
        The logic for incrementally syncing threads is not very obvious, so buckle up.

        To get all messages in a thread, one must specify the channel and timestamp of the parent (first) message of that thread, basically its ID.

        One complication is that threads can be updated at any time in the future. Therefore, if we wanted to comprehensively sync data i.e: get every
        single response in a thread, we'd have to read every message in the slack instance every time we ran a sync, because otherwise there is no
        way to guarantee that a thread deep in the past didn't receive a new message.

        A pragmatic workaround is to say we want threads to be at least N days fresh i.e: look back N days into the past, get every message since,
        and read all of the thread responses. This is essentially the approach we're taking here via slicing: create slices from N days into the
        past and read all messages in threads since then. We could optionally filter out records we have already read, but that's omitted to keep
        the logic simple to reason about.

        Good luck.
        """

        stream_state = stream_state or {}
        channels_stream = Channels(authenticator=self.authenticator)
        if "start_ts" in stream_state:
            # Since new messages can be posted to threads continuously after the parent message has been posted, we get messages from the latest date
            # found in the state minus 7 days to pick up any new messages in threads.
            # If there is state always use lookback
            messages_start_date = pendulum.from_timestamp(stream_state.get("start_ts")).subtract(**self.messages_lookback_window)
        else:
            # If there is no state i.e: this is the first sync then there is no use for lookback, just get messages from the default start date
            messages_start_date = self._default_start_date
        messages_stream = ChannelMessages(authenticator=self.authenticator, default_start_date=messages_start_date)

        for message_chunk in messages_stream.stream_slices(stream_state={"start_ts": messages_start_date.timestamp()}):
            for channel in channels_stream.read_records(sync_mode=SyncMode.full_refresh):
                message_chunk["channel"] = channel["id"]
                for message in messages_stream.read_records(sync_mode=SyncMode.full_refresh, stream_slice=message_chunk):
                    yield {"channel": channel["id"], "ts": message["ts"]}


class JoinChannelsStream(HttpStream):
    """
    This class is a special stream which joins channels because the Slack API only returns messages from channels this bot is in.
    Its responses should only be logged for debugging reasons, not read as records.
    """

    url_base = "https://slack.com/api/"
    http_method = "POST"

    def parse_response(self, response: requests.Response, stream_slice: Mapping[str, Any] = None, **kwargs) -> Iterable[Mapping]:
        return [{"message": f"Successfully joined channel: {stream_slice['channel_name']}"}]

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        return None  # No pagination

    def path(self, **kwargs) -> str:
        return "conversations.join"

    def stream_slices(self, **kwargs) -> Iterable[Optional[Mapping[str, any]]]:
        channels_stream = Channels(authenticator=self.authenticator)
        for channel in channels_stream.read_records(sync_mode=SyncMode.full_refresh):
            yield {"channel": channel["id"], "channel_name": channel["name"]}

    def request_body_json(self, stream_slice: Mapping = None, **kwargs) -> Optional[Mapping]:
        return {"channel": stream_slice["channel"]}


class SourceSlack(AbstractSource):
    def check_connection(self, logger: AirbyteLogger, config: Mapping[str, Any]) -> Tuple[bool, Optional[Any]]:
        slack_client = WebClient(token=config["api_token"])
        users = slack_client.users_list(limit=1).get("members", [])
        if len(users) > 0:
            return True, None
        else:
            return False, "There are no users in the given Slack instance"

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        authenticator = TokenAuthenticator(config["api_token"])
        default_start_date = pendulum.now().subtract(days=14)  # TODO make this configurable
        threads_lookback_window = {"days": 7}  # TODO make this configurable

        streams = [
            Channels(authenticator=authenticator),
            ChannelMembers(authenticator=authenticator),
            ChannelMessages(authenticator=authenticator, default_start_date=default_start_date),
            Threads(authenticator=authenticator, default_start_date=default_start_date, lookback_window=threads_lookback_window),
            Users(authenticator=authenticator),
        ]

        # To sync data from channels, the bot backed by this token needs to join all those channels. This operation is idempotent.
        # TODO make joining configurable. Also make joining archived and private channels configurable
        logger = AirbyteLogger()
        logger.info("joining Slack channels")
        join_channels_stream = JoinChannelsStream(authenticator=authenticator)
        for stream_slice in join_channels_stream.stream_slices():
            for message in join_channels_stream.read_records(sync_mode=SyncMode.full_refresh, stream_slice=stream_slice):
                logger.info(message["message"])

        return streams
