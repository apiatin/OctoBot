#  This file is part of OctoBot (https://github.com/Drakkar-Software/OctoBot)
#  Copyright (c) 2021 Drakkar-Software, All rights reserved.
#
#  OctoBot is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either
#  version 3.0 of the License, or (at your option) any later version.
#
#  OctoBot is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  General Public License for more details.
#
#  You should have received a copy of the GNU General Public
#  License along with OctoBot. If not, see <https://www.gnu.org/licenses/>.
import gmqtt
import json
import zlib
import distutils.version as loose_version

import octobot_commons.enums as commons_enums
import octobot_commons.errors as commons_errors
import octobot.community.feeds.abstract_feed as abstract_feed
import octobot.constants as constants


class CommunityMQTTFeed(abstract_feed.AbstractFeed):
    MQTT_VERSION = gmqtt.constants.MQTTv311
    MQTT_BROKER_PORT = 1883

    # Quality of Service level determines the reliability of the data flow between a client and a message broker.
    # The message may be sent in three ways:
    # QoS 0: the message will be received at most once (also known as “fire and forget”).
    # QoS 1: the message will be received at least once.
    # QoS 2: the message will be received exactly once.
    # from https://www.scaleway.com/en/docs/iot/iot-hub/concepts/#quality-of-service-levels-(qos)
    default_QOS = 1

    def __init__(self, feed_url, authenticator):
        super().__init__(feed_url, authenticator)
        self.mqtt_version = self.MQTT_VERSION
        self.mqtt_broker_port = self.MQTT_BROKER_PORT
        self.default_QOS = self.default_QOS

        self._mqtt_client: gmqtt.Client = None
        self._device_credential: str = None
        self._subscription_topics = set()

    async def start(self):
        await self._fetch_device_credential()
        await self._connect()

    async def stop(self):
        if self._mqtt_client is not None and self._mqtt_client.is_connected:
            await self._mqtt_client.disconnect()

    async def register_feed_callback(self, channel_type, callback, identifier=None):
        topic = self._build_topic(channel_type, identifier)
        try:
            self.feed_callbacks[topic].append(callback)
        except KeyError:
            self.feed_callbacks[topic] = [callback]
        if identifier not in self._subscription_topics:
            self._subscription_topics.add(topic)
            self._subscribe((topic, ))

    async def _fetch_device_credential(self):
        # TODO
        self._device_credential = "a8e68204-4c51-47c0-bb22-b24fae14d546"

        return
        device_creds_fetch_url = None
        async with self.authenticator.get_aiohttp_session().get(device_creds_fetch_url) as resp:
            if resp.status == 200:
                self._device_credential = await resp.json()
            else:
                raise RuntimeError(f"Error when fetching device creds: status: {resp.status}, "
                                   f"text: {await resp.text()}")

    @staticmethod
    def _build_topic(channel_type, identifier):
        return f"{channel_type.value}/{identifier}"

    async def _on_message(self, client, topic, payload, qos, properties):
        self.logger.debug(f"RECV MSG {client._client_id} TOPIC: {topic,} PAYLOAD: {payload} "
                          f"QOS: {qos} PROPERTIES: {properties}")
        parsed_message = json.loads(zlib.decompress(payload).decode())
        try:
            self._ensure_supported(parsed_message)
            for callback in self._get_callbacks(topic):
                await callback(parsed_message)
        except commons_errors.UnsupportedError as e:
            self.logger.error(f"Unsupported message: {e}")

    async def send(self, message, channel_type, identifier, **kwargs):
        self._mqtt_client.publish(
            self._build_topic(channel_type, identifier),
            self._build_message(channel_type, message),
            qos=self.default_QOS
        )

    def _get_callbacks(self, topic):
        for callback in self.feed_callbacks.get(topic, ()):
            yield callback

    def _get_channel_type(self, message):
        return commons_enums.CommunityChannelTypes(message[commons_enums.CommunityFeedAttrs.CHANNEL_TYPE.value])

    def _build_message(self, channel_type, message):
        if message:
            return zlib.compress(
                json.dumps({
                    commons_enums.CommunityFeedAttrs.CHANNEL_TYPE.value: channel_type,
                    commons_enums.CommunityFeedAttrs.VERSION.value: constants.COMMUNITY_FEED_CURRENT_MINIMUM_VERSION,
                    commons_enums.CommunityFeedAttrs.VALUE.value: message,
                }).encode()
            )
        return {}

    def _ensure_supported(self, parsed_message):
        if loose_version.LooseVersion(parsed_message[commons_enums.CommunityFeedAttrs.VERSION.value]) \
                < loose_version.LooseVersion(constants.COMMUNITY_FEED_CURRENT_MINIMUM_VERSION):
            raise commons_errors.UnsupportedError(
                f"Minimum version: {constants.COMMUNITY_FEED_CURRENT_MINIMUM_VERSION}"
            )

    def _on_connect(self, client, flags, rc, properties):
        self.logger.info(f"CONNECTED, client_id={client._client_id}")
        # Auto subscribe to known topics (mainly used in case of reconnection)
        self._subscribe(self._subscription_topics)

    def _on_disconnect(self, client, packet, exc=None):
        self.logger.info(f"DISCONNECTED, client_id={client._client_id}")

    def _on_subscribe(self, client, mid, qos, properties):
        # from https://github.com/wialon/gmqtt/blob/master/examples/resubscription.py#L28
        # in order to check if all the subscriptions were successful, we should first get all subscriptions with this
        # particular mid (from one subscription request)
        subscriptions = client.get_subscriptions_by_mid(mid)
        for subscription, granted_qos in zip(subscriptions, qos):
            # in case of bad suback code, we can resend  subscription
            if granted_qos >= gmqtt.constants.SubAckReasonCode.UNSPECIFIED_ERROR.value:
                self.logger.warning(f"[RETRYING SUB {client._client_id}] mid {mid,}, "
                                    f"reason code: {granted_qos}, properties {properties}")
                client.resubscribe(subscription)
            self.logger.info(f"[SUBSCRIBED {client._client_id}] mid {mid}, QOS: {granted_qos}, properties {properties}")

    def _register_callbacks(self, client):
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.on_subscribe = self._on_subscribe

    async def _connect(self):
        self._mqtt_client = gmqtt.Client(self.__class__.__name__)
        self._register_callbacks(self._mqtt_client)
        self._mqtt_client.set_auth_credentials(self._device_credential, None)
        self.logger.debug(f"Connecting client")
        await self._mqtt_client.connect(self.feed_url, self.mqtt_broker_port, version=self.MQTT_VERSION)

    def _subscribe(self, topics):
        if not topics:
            self.logger.debug("No topic to subscribe to, skipping subscribe for now")
            return
        subscriptions = [
            gmqtt.Subscription(topic, qos=self.default_QOS)
            for topic in topics
        ]
        self.logger.debug(f"Subscribing to {','.join(topics)}")
        self._mqtt_client.subscribe(subscriptions)
