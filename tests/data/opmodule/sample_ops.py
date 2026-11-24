"""A module of Python operations for the `requires` tests: imported only by a pipeline file."""
import slideio_processor as sp

__version__ = "0.3"


class Grey(sp.Operation):
    name = "sample.grey"
    version = "1"
    output = sp.Output.RASTER

    def apply(self, tile, context):
        return tile.mean(axis=2, dtype="float32")
