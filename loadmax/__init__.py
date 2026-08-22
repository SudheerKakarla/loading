"""LoadMax: container and truck loading optimizer."""

from loadmax.models import Load_Obj, Pallet_Obj
from loadmax.data_reader import read_im_input_csv
from loadmax.container_loading_highs import (
    ContainerLoadingOptimizerHiGHS,
    OptModelHiGHS,
)

try:
    from loadmax.optimizer import ContainerLoadingOptimizerVDR, OptModel
except ImportError:  # gurobipy is optional when running HiGHS only
    ContainerLoadingOptimizerVDR = None
    OptModel = None
from loadmax.results_summary import (
    build_container_results_summary,
    print_container_results_summary,
    write_container_results_summary,
)

__all__ = [
    "ContainerLoadingOptimizerVDR",
    "OptModel",
    "ContainerLoadingOptimizerHiGHS",
    "OptModelHiGHS",
    "Load_Obj",
    "Pallet_Obj",
    "read_im_input_csv",
    "build_container_results_summary",
    "print_container_results_summary",
    "write_container_results_summary",
]
