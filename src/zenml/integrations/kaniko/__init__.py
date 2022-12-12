#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""Kaniko integration for image building."""
from typing import List, Type

from zenml.integrations.constants import KANIKO
from zenml.integrations.integration import Integration
from zenml.stack import Flavor

KANIKO_IMAGE_BUILDER_FLAVOR = "kaniko"


class KanikoIntegration(Integration):
    """Definition of the Kaniko integration for ZenML."""

    NAME = KANIKO
    REQUIREMENTS = ["kubernetes==18.20.0"]

    @classmethod
    def flavors(cls) -> List[Type[Flavor]]:
        """Declare the stack component flavors for the Kaniko integration.

        Returns:
            List of new stack component flavors.
        """
        from zenml.integrations.kaniko.flavors import KanikoImageBuilderFlavor

        return [KanikoImageBuilderFlavor]


KanikoIntegration.check_installation()
