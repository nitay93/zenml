#  Copyright (c) ZenML GmbH 2021. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
from typing import Optional

from tensorflow import keras

from zenml.materializers.base_materializer import BaseMaterializer

DEFAULT_FILENAME = "model.hdf5"


class KerasMaterializer(BaseMaterializer):
    """Materializer to read/write Keras models."""

    TYPE_NAME = "keras"

    def read_model(self) -> Optional[keras.Model]:
        """Reads and returns a Keras model."""
        return keras.models.load_model(self.artifact.uri)

    def write_model(self, model: keras.Model) -> None:
        """Writes a keras model."""
        model.save(self.artifact.uri)
