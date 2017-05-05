#!/usr/bin/python
#
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

import os
import sys
import subprocess

sys.path.append('hooks/')

from charmhelpers.core.hookenv import (
    action_fail,
    action_set,
    action_get,
)

from rabbit_utils import (
    ConfigRenderer,
    CONFIG_FILES,
    pause_unit_helper,
    resume_unit_helper,
)


def pause(args):
    """Pause the RabbitMQ services.
    @raises Exception should the service fail to stop.
    """
    pause_unit_helper(ConfigRenderer(CONFIG_FILES))


def resume(args):
    """Resume the RabbitMQ services.
    @raises Exception should the service fail to start."""
    resume_unit_helper(ConfigRenderer(CONFIG_FILES))


def cluster_status(args):
    try:
        clusterstat = subprocess.check_output(['rabbitmqctl', 'cluster_status'])
        action_set({'output': clusterstat, 'outcome': 'Success'})
    except subprocess.CalledProcessError as e:
        action_set({'output': e.output})
        action_fail('Failed to run rabbitmqctl cluster_status')


# A dictionary of all the defined actions to callables (which take
# parsed arguments).
ACTIONS = {"pause": pause, "resume": resume, "cluster-status": cluster_status}


def main(args):
    action_name = os.path.basename(args[0])
    try:
        action = ACTIONS[action_name]
    except KeyError:
        s = "Action {} undefined".format(action_name)
        action_fail(s)
        return s
    else:
        try:
            action(args)
        except Exception as e:
            action_fail("Action {} failed: {}".format(action_name, str(e)))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
