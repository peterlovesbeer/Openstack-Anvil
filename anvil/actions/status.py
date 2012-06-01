# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from anvil import colorizer
from anvil import log

from anvil.actions import base

from anvil.actions.base import PhaseFunctors

LOG = log.getLogger(__name__)


class StatusAction(base.Action):

    @staticmethod
    def get_lookup_name():
        return 'running'

    @staticmethod
    def get_action_name():
        return 'status'

    def _fetch_status(self, component):
        return component.status()

    def _print_status(self, component, status):
        LOG.info("Status of %s is %s", colorizer.quote(i.component_name), colorizer.quote(str(status)))

    def _run(self, persona, component_order, instances):
        self._run_phase(
            PhaseFunctors(
                start=lambda i: LOG.info('Getting status of %s.', colorizer.quote(i.component_name)),
                run=self._fetch_status,
                end=self._print_status,
            ),
            component_order,
            instances,
            None,
            )
