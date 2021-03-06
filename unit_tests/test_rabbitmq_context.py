# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import rabbitmq_context

import mock
import unittest


class TestRabbitMQSSLContext(unittest.TestCase):

    @mock.patch("rabbitmq_context.config")
    @mock.patch("rabbitmq_context.close_port")
    @mock.patch("rabbitmq_context.ssl_utils.reconfigure_client_ssl")
    @mock.patch("rabbitmq_context.ssl_utils.get_ssl_mode")
    def test_context_ssl_off(self, get_ssl_mode, reconfig_ssl, close_port,
                             config):
        get_ssl_mode.return_value = ("off", "off")
        self.assertEqual(rabbitmq_context.RabbitMQSSLContext().__call__(), {
            "ssl_mode": "off"
        })

        self.assertTrue(close_port.called)
        self.assertTrue(reconfig_ssl.called)

    @mock.patch("rabbitmq_context.open_port")
    @mock.patch("rabbitmq_context.os.chmod")
    @mock.patch("rabbitmq_context.os.chown")
    @mock.patch("rabbitmq_context.os.path.exists")
    @mock.patch("rabbitmq_context.pwd.getpwnam")
    @mock.patch("rabbitmq_context.grp.getgrnam")
    @mock.patch("rabbitmq_context.config")
    @mock.patch("rabbitmq_context.close_port")
    @mock.patch("rabbitmq_context.ssl_utils.reconfigure_client_ssl")
    @mock.patch("rabbitmq_context.ssl_utils.get_ssl_mode")
    def test_context_ssl_on(self, get_ssl_mode, reconfig_ssl, close_port,
                            config, gr, pw, exists, chown, chmod, open_port):

        exists.return_value = True
        get_ssl_mode.return_value = ("on", "on")

        def config_get(n):
            return None

        config.side_effect = config_get

        def pw(name):
            class Uid(object):
                pw_uid = 1
                gr_gid = 100
            return Uid()

        pw.side_effect = pw
        gr.side_effect = pw

        m = mock.mock_open()
        with mock.patch('rabbitmq_context.open', m, create=True):
            self.assertEqual(
                rabbitmq_context.RabbitMQSSLContext().__call__(), {
                    "ssl_port": None,
                    "ssl_cert_file": "/etc/rabbitmq/rabbit-server-cert.pem",
                    "ssl_key_file": '/etc/rabbitmq/rabbit-server-privkey.pem',
                    "ssl_client": False,
                    "ssl_ca_file": "",
                    "ssl_only": False,
                    "ssl_mode": "on",
                })

        self.assertTrue(reconfig_ssl.called)
        self.assertTrue(open_port.called)


class TestRabbitMQClusterContext(unittest.TestCase):

    @mock.patch("rabbitmq_context.config")
    def test_context_ssl_off(self, config):
        config.return_value = "ignore"

        self.assertEqual(
            rabbitmq_context.RabbitMQClusterContext().__call__(), {
                'cluster_partition_handling': "ignore"
            })

        config.assert_called_once_with("cluster-partition-handling")
