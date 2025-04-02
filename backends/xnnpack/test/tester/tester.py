# Copyright (c) Meta Platforms, Inc. and affiliates.
# Copyright 2024-2025 Arm Limited and/or its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import copy

import logging
import random
import sys
from abc import ABC, abstractmethod
from collections import Counter, OrderedDict
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Type, Union

import torch
from executorch.backends.transforms.duplicate_dynamic_quant_chain import (
    DuplicateDynamicQuantChainPass,
)
from executorch.backends.xnnpack._passes import XNNPACKPassManager
from executorch.backends.xnnpack.partition.xnnpack_partitioner import XnnpackPartitioner
from executorch.backends.xnnpack.utils.configs import get_xnnpack_edge_compile_config
from executorch.exir import (
    EdgeCompileConfig,
    EdgeProgramManager,
    ExecutorchBackendConfig,
    ExecutorchProgramManager,
    to_edge,
    to_edge_transform_and_lower,
)
from executorch.exir.backend.backend_api import validation_disabled
from executorch.exir.backend.partitioner import Partitioner
from executorch.exir.passes.sym_shape_eval_pass import ConstraintBasedSymShapeEvalPass

from executorch.exir.print_program import pretty_print, print_program
from torch.export import export_for_training

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
try:
    from executorch.extension.pybindings.portable_lib import (  # @manual
        _load_for_executorch_from_buffer,
    )
except ImportError as e:
    logger.warning(f"{e=}")
    pass

from executorch.backends.xnnpack.quantizer.xnnpack_quantizer import (
    get_symmetric_quantization_config,
    XNNPACKQuantizer,
)
from executorch.backends.xnnpack.quantizer.xnnpack_quantizer_utils import (
    QuantizationConfig,
)
from executorch.exir.program._program import _transform
from torch._export.pass_base import PassType
from torch.ao.quantization.quantize_pt2e import convert_pt2e, prepare_pt2e
from torch.ao.quantization.quantizer.quantizer import Quantizer
from torch.export import export, ExportedProgram
from torch.testing import FileCheck
from torch.utils._pytree import tree_flatten


class Stage(ABC):
    """
    Interface for a Stage in the PT2.0 lowering pipeline
    """

    @abstractmethod
    def run(self, artifact, inputs):
        """
        Executes this stage, generates the 'artifact', for later stages.
        """
        pass

    @property
    @abstractmethod
    def artifact(self):
        """
        Returns the artifact generated by this stage. To be used by the next stage in the pipeline.
        """
        pass

    @property
    @abstractmethod
    def graph_module(self):
        """
        Return the artifact's graph module for this stage
        """
        pass

    def run_artifact(self, inputs):
        """
        Returns the output of calling the artifact generated by this stage with inputs
        """
        if isinstance(self.artifact, ExportedProgram):
            return self.artifact(*inputs)
        else:
            return self.artifact.exported_program().module()(*inputs)

    # Debug Tools for stages
    def artifact_str(self):
        """
        Return string printable artifact for this stage
        """
        if isinstance(self.artifact, EdgeProgramManager):
            return self.artifact.exported_program()
        return self.artifact

    def stage_banner(self):
        """
        Returns banner string for this stage
        """
        return "#" * 36 + " " + str(self.__class__.__name__) + " " + "#" * 36 + "\n"

    def dump_artifact(self, path_to_dump: Optional[str]):
        """
        Dumps string printable artifact to path. If path_to_dump, then it is printed to terminal
        """
        if path_to_dump:
            with open(path_to_dump, "a") as fp:
                fp.write(str(self.stage_banner() + "\n"))
                fp.write(str(self.artifact_str()))
        else:
            print(self.stage_banner() + "\n")
            print(self.artifact_str())


_stages_: Dict[str, Stage] = {}


def register_stage(stage: Stage):
    """
    Register a Stage to be used in the Tester.
    """
    assert isinstance(stage, type)
    name = stage.__qualname__
    if name in _stages_:
        raise RuntimeError(f"Duplicate stage in Tester, {name}")
    _stages_[name] = stage
    return stage


@register_stage
class Quantize(Stage):
    def __init__(
        self,
        quantizer: Optional[Quantizer] = None,
        quantization_config: Optional[QuantizationConfig] = None,
        calibrate: bool = True,
        calibration_samples: Optional[Sequence[Any]] = None,
    ):
        self.quantizer = quantizer or XNNPACKQuantizer()
        self.quantization_config = (
            quantization_config or get_symmetric_quantization_config()
        )
        self.calibrate = calibrate
        self.calibration_samples = calibration_samples

        self.quantizer.set_global(self.quantization_config)

        self.converted_graph = None

    def run(
        self, artifact: torch.nn.Module, inputs: Optional[Tuple[torch.Tensor]]
    ) -> None:
        assert inputs is not None
        captured_graph = export_for_training(artifact, inputs, strict=True).module()

        assert isinstance(captured_graph, torch.fx.GraphModule)
        prepared = prepare_pt2e(captured_graph, self.quantizer)

        if self.calibrate:
            # Calibrate prepared model to provide data to quantization observers.
            if self.calibration_samples is not None:
                for inp in self.calibration_samples:
                    prepared(*inp)
            else:
                prepared(*inputs)

        converted = convert_pt2e(prepared)
        DuplicateDynamicQuantChainPass()(converted)

        self.converted_graph = converted

    @property
    def artifact(self) -> torch.fx.GraphModule:
        return self.converted_graph

    @property
    def graph_module(self) -> str:
        return self.converted_graph

    def run_artifact(self, inputs):
        return self.converted_graph.forward(*inputs)


@register_stage
class Export(Stage):
    def __init__(self, dynamic_shapes: Optional[Tuple[Any]] = None):
        self.exported_program = None
        self.dynamic_shapes = dynamic_shapes

    def run(
        self,
        artifact: torch.nn.Module,
        inputs: Tuple[torch.Tensor],
    ) -> None:
        self.exported_program = export(
            artifact, inputs, dynamic_shapes=self.dynamic_shapes, strict=True
        )

    @property
    def artifact(self) -> ExportedProgram:
        return self.exported_program

    @property
    def graph_module(self) -> str:
        return self.exported_program.graph_module


@register_stage
class ToEdge(Stage):
    def __init__(self, edge_compile_config: Optional[EdgeCompileConfig] = None):
        self.edge_compile_conf = (
            edge_compile_config or get_xnnpack_edge_compile_config()
        )
        self.edge_dialect_program = None

    def run(self, artifact: ExportedProgram, inputs=None) -> None:
        self.edge_dialect_program = to_edge(
            artifact, compile_config=self.edge_compile_conf
        )

    @property
    def artifact(self) -> EdgeProgramManager:
        return self.edge_dialect_program

    @property
    def graph_module(self) -> str:
        return self.edge_dialect_program.exported_program().graph_module


@register_stage
class RunPasses(Stage):
    def __init__(
        self,
        pass_list: Optional[List[Type[PassType]]] = None,
        pass_functions: Optional[List[Callable]] = None,
    ):
        self.pass_list = pass_list
        self.pass_functions = pass_functions
        self.edge_or_aten_program = None

    def run(
        self, artifact: Union[EdgeProgramManager, ExportedProgram], inputs=None
    ) -> None:
        if isinstance(artifact, EdgeProgramManager):
            self.edge_or_aten_program = artifact
            if self.pass_list:
                pass_manager = XNNPACKPassManager(
                    artifact.exported_program(), self.pass_list
                )
                self.edge_or_aten_program._edge_programs["forward"] = (
                    pass_manager.transform()
                )
            if self.pass_functions:
                assert isinstance(self.pass_functions, list)
                for pass_function in self.pass_functions:
                    self.edge_or_aten_program._edge_programs["forward"] = pass_function(
                        self.edge_or_aten_program.exported_program()
                    )
        else:
            transformed_ep = artifact
            if self.pass_list:
                assert isinstance(self.pass_list, list)
                for pass_ in self.pass_list:
                    transformed_ep = _transform(transformed_ep, pass_())

            if self.pass_functions:
                assert isinstance(self.pass_functions, list)
                for pass_function in self.pass_functions:
                    transformed_ep = pass_function(transformed_ep)

            self.edge_or_aten_program = transformed_ep

    @property
    def artifact(self) -> Union[EdgeProgramManager, ExportedProgram]:
        return self.edge_or_aten_program

    @property
    def graph_module(self) -> str:
        if isinstance(self.edge_or_aten_program, EdgeProgramManager):
            return self.edge_or_aten_program.exported_program().graph_module
        else:
            return self.edge_or_aten_program.graph_module


@register_stage
class ToEdgeTransformAndLower(Stage):
    def __init__(
        self,
        partitioners: Optional[List[Partitioner]] = None,
        edge_compile_config: Optional[EdgeCompileConfig] = None,
    ):
        self.partitioners = partitioners or [XnnpackPartitioner()]
        self.edge_compile_conf = (
            edge_compile_config or get_xnnpack_edge_compile_config()
        )
        self.edge_dialect_program = None

    def run(self, artifact: ExportedProgram, inputs=None) -> None:
        self.edge_dialect_program = to_edge_transform_and_lower(
            artifact,
            compile_config=self.edge_compile_conf,
            partitioner=self.partitioners,
        )

    @property
    def artifact(self) -> EdgeProgramManager:
        return self.edge_dialect_program

    @property
    def graph_module(self) -> str:
        return self.edge_dialect_program.exported_program().graph_module


@register_stage
class Partition(Stage):
    def __init__(self, partitioner: Optional[Partitioner] = None):
        self.partitioner = partitioner or XnnpackPartitioner()
        self.delegate_module = None

    def run(self, artifact: EdgeProgramManager, inputs=None):
        with validation_disabled():
            self.delegate_module = artifact
            self.delegate_module = self.delegate_module.to_backend(self.partitioner)

    @property
    def artifact(self) -> EdgeProgramManager:
        return self.delegate_module

    @property
    def graph_module(self) -> str:
        return self.delegate_module.exported_program().graph_module


@register_stage
class ToExecutorch(Stage):
    def __init__(
        self,
        config: Optional[ExecutorchBackendConfig] = None,
    ):
        self.config = config or ExecutorchBackendConfig(
            extract_delegate_segments=True,
            sym_shape_eval_pass=ConstraintBasedSymShapeEvalPass(),
        )
        self.executorch_program = None

    def run(self, artifact: EdgeProgramManager, inputs=None):
        self.executorch_program = artifact.to_executorch(self.config)

    @property
    def artifact(self) -> ExecutorchProgramManager:
        return self.executorch_program

    @property
    def graph_module(self) -> str:
        return self.executorch_program().graph_module

    def dump_artifact(self, path_to_dump: Optional[str]):
        """
        dump_artifact is overridden to dump the serialized program
        """
        original_stdout = sys.stdout

        sys.stdout = open(path_to_dump, "a") if path_to_dump else sys.stdout
        print(self.stage_banner() + "\n")
        pretty_print(self.artifact._emitter_output.program)
        print_program(
            self.artifact._emitter_output.program,
            show_meminfo=True,
            mark_dynamic_shape_tensor=True,
        )
        sys.stdout = original_stdout


@register_stage
class Serialize(Stage):
    def __init__(self):
        self.buffer = None

    def run(self, artifact: ExecutorchProgramManager, inputs=None) -> None:
        self.buffer = artifact.buffer

    @property
    def artifact(self) -> bytes:
        return self.buffer

    @property
    def graph_module(self) -> None:
        return None

    def run_artifact(self, inputs):
        inputs_flattened, _ = tree_flatten(inputs)
        executorch_module = _load_for_executorch_from_buffer(self.buffer)
        executorch_output = copy.deepcopy(
            executorch_module.run_method("forward", tuple(inputs_flattened))
        )
        return executorch_output

    def dump_artifact(self, path_to_dump: Optional[str]):
        """
        dump_artifact is overridden to dump the serialized bytes into pte file
        """
        if not path_to_dump:
            raise RuntimeError("path_to_dump file not provided")
        else:
            with open(path_to_dump, "wb") as f:
                f.write(self.artifact)


class Tester:
    def __init__(
        self,
        module: torch.nn.Module,
        example_inputs: Tuple[torch.Tensor],
        dynamic_shapes: Optional[Tuple[Any]] = None,
    ):
        module.eval()

        self.original_module = module
        self.example_inputs = example_inputs
        self.dynamic_shapes = dynamic_shapes
        self.stages: Dict[str, Stage] = OrderedDict.fromkeys(list(_stages_.keys()))
        self.pipeline = {
            self.stage_name(Quantize): [self.stage_name(Export)],
            self.stage_name(Export): [
                self.stage_name(RunPasses),
                self.stage_name(ToEdge),
                self.stage_name(ToEdgeTransformAndLower),
            ],
            self.stage_name(ToEdgeTransformAndLower): [
                self.stage_name(RunPasses),
                self.stage_name(ToExecutorch),
            ],
            self.stage_name(ToEdge): [
                self.stage_name(Partition),
                self.stage_name(RunPasses),
            ],
            self.stage_name(RunPasses): [
                self.stage_name(Partition),
                self.stage_name(ToEdgeTransformAndLower),
            ],
            # TODO Make this Stage optional
            self.stage_name(Partition): [self.stage_name(ToExecutorch)],
            self.stage_name(ToExecutorch): [self.stage_name(Serialize)],
            self.stage_name(Serialize): [],
        }
        assert all(
            stage in self.pipeline for stage in self.stages
        ), "Invalid Tester internal state!"

        # Current stage name
        self.cur: str = ""

        # Reference output from eager mode
        self.reference_output = None

        # Quantization scale from eager mode
        self.quantization_scale: Optional[float] = None

        # Artifact output from stage
        self.stage_output = None

    def generate_random_inputs(self):
        # Get shapes of inputs
        input_shapes = []
        if self.dynamic_shapes is None:
            for tensor_arg in self.example_inputs:
                assert isinstance(tensor_arg, torch.Tensor)
                input_shapes.append(tensor_arg.shape)
        else:
            # Random shapes depending on dynamic shape constraint
            dim_name_to_size = {}
            for arg_idx in range(len(self.example_inputs)):
                assert isinstance(self.example_inputs[arg_idx], torch.Tensor)
                ex_shape = list(self.example_inputs[arg_idx].shape)
                dynamic_dim_spec = self.dynamic_shapes[arg_idx]
                for dim_idx, dim_spec in dynamic_dim_spec.items():
                    assert dim_idx < len(ex_shape)
                    if isinstance(dim_spec, torch.export.dynamic_shapes._DerivedDim):
                        # derived dims are of the form {0: 2 * torch.export.Dim() // 2}
                        # The root contains the min/max of the export dim and fn contains
                        # the function to compute the derived dim.
                        dim_spec = dim_spec.root
                        fn = dim_spec.fn
                    elif isinstance(dim_spec, torch.export.dynamic_shapes._Dim):
                        # Not derived dim so fn is just itself
                        def fn(x):
                            return x

                    else:
                        raise RuntimeError(
                            f"Expected Dynamic Dims to be of type _DerivedDim or _Dim but got {type(dim_spec)}"
                        )
                    dim_name = dim_spec.__name__
                    if dim_name not in dim_name_to_size:
                        upper_bound = min(
                            dim_spec.max, 1000
                        )  # unbounded int max is too large
                        lower_bound = (
                            dim_spec.min if dim_spec.min >= 2 else 1
                        )  # 0/1 specialization means dim_spec.min can never be 1
                        dim_name_to_size[dim_name] = fn(
                            random.randint(lower_bound, upper_bound)
                        )
                    ex_shape[dim_idx] = dim_name_to_size[dim_spec.__name__]
                input_shapes.append(torch.Size(ex_shape))
        # create random tensor inputs with the shapes given above:
        random_inputs = []
        for arg_idx in range(len(self.example_inputs)):
            random_inputs.append(
                torch.randn(input_shapes[arg_idx]).to(
                    dtype=self.example_inputs[arg_idx].dtype
                )
            )

        yield tuple(random_inputs)

    @staticmethod
    def stage_name(stage) -> str:
        t = stage if isinstance(stage, type) else type(stage)
        return t.__qualname__

    def _pre(self, stage):
        name: str = self.stage_name(stage)
        assert isinstance(name, str) and name in self.stages and not self.stages[name]

        last_artifact = self.original_module
        if self.cur:
            assert self.cur in self.pipeline, f"Invalid state: {self.cur}"
            allowed_next_stages = self.pipeline[self.cur]
            assert name in allowed_next_stages, f"Invalid next stage: {name}"
            last_artifact = self.get_artifact()
        self.cur = name
        return last_artifact

    def _post(self, stage):
        name = self.stage_name(stage)
        assert name in self.stages
        self.stages[name] = stage

    def _run_stage(self, stage_instance, inputs=None):
        assert isinstance(stage_instance, Stage)
        prev_stage_artifact = self._pre(stage_instance)
        stage_instance.run(prev_stage_artifact, inputs=inputs)
        self._post(stage_instance)
        return self

    # Stages
    def quantize(self, quantize_stage: Optional[Quantize] = None):
        return self._run_stage(quantize_stage or Quantize(), self.example_inputs)

    def export(self, export_stage: Optional[Export] = None):
        return self._run_stage(
            export_stage or Export(dynamic_shapes=self.dynamic_shapes),
            self.example_inputs,
        )

    def to_edge(self, to_edge_stage: Optional[ToEdge] = None):
        if not to_edge_stage:
            to_edge_stage = ToEdge()
        res = self._run_stage(to_edge_stage)
        return res

    def to_edge_transform_and_lower(
        self, to_edge_and_transform_stage: Optional[ToEdgeTransformAndLower] = None
    ):
        return self._run_stage(to_edge_and_transform_stage or ToEdgeTransformAndLower())

    def run_passes(self, run_passes_stage: Optional[RunPasses] = None):
        return self._run_stage(run_passes_stage or RunPasses())

    def partition(self, partition_stage: Optional[Partition] = None):
        return self._run_stage(partition_stage or Partition())

    def to_executorch(self, to_executorch_stage: Optional[ToExecutorch] = None):
        return self._run_stage(to_executorch_stage or ToExecutorch())

    def serialize(self, serialize_stage: Optional[Serialize] = None):
        return self._run_stage(serialize_stage or Serialize())

    # Util functions
    def dump_artifact(self, path: Optional[str] = None, stage: Optional[str] = None):
        stage = stage or self.cur
        self.stages[stage].dump_artifact(path)
        return self

    def get_artifact(self, stage: Optional[str] = None):
        stage = stage or self.cur
        return self.stages[stage].artifact

    def check(self, input: List[str]):
        for key in input:
            FileCheck().check(key).run(self.stages[self.cur].graph_module.code)
        return self

    def check_not(self, input: List[str]):
        for key in input:
            FileCheck().check_not(key).run(self.stages[self.cur].graph_module.code)
        return self

    def check_count(self, input: Dict[Any, int]):
        # TODO target checks similar to checkGraphModuleNodes()
        for key, count in input.items():
            FileCheck().check_count(key, count, exactly=True).run(
                self.stages[self.cur].graph_module.code
            )
        return self

    def check_node_count(self, input: Dict[Any, int]):
        # Count the occurances of each target in the graph.
        target_ops = [
            node.target
            for node in self.stages[self.cur].graph_module.graph.nodes
            if node.op == "call_function"
        ]
        op_counts = Counter(target_ops)

        for key, count in input.items():
            if count != op_counts[key]:
                print(f"Nodes: {op_counts}")
                raise AssertionError(
                    f"Expected {count} {key} nodes but found {op_counts[key]}."
                )

        return self

    def visualize(
        self, reuse_server: bool = True, stage: Optional[str] = None, **kwargs
    ):
        # import here to avoid importing model_explorer when it is not needed which is most of the time.
        from executorch.devtools.visualization import visualize

        visualize(self.get_artifact(stage), reuse_server=reuse_server, **kwargs)
        return self

    def run_method_and_compare_outputs(
        self,
        stage: Optional[str] = None,
        inputs: Optional[Tuple[torch.Tensor]] = None,
        num_runs=1,
        atol=1e-03,
        rtol=1e-03,
        qtol=0,
    ):
        number_of_runs = 1 if inputs is not None else num_runs
        reference_stage = self.stages[self.stage_name(Export)]

        stage = stage or self.cur

        print(f"Comparing Stage {stage} with Stage {reference_stage}")
        for run_iteration in range(number_of_runs):
            inputs_to_run = inputs if inputs else next(self.generate_random_inputs())
            input_shapes = [generated_input.shape for generated_input in inputs_to_run]
            print(f"Run {run_iteration} with input shapes: {input_shapes}")

            # Reference output (and quantization scale)
            (
                reference_output,
                quantization_scale,
            ) = self._calculate_reference_output(
                reference_stage.artifact, inputs_to_run
            )

            # Output from running artifact at stage
            stage_output = self.stages[stage].run_artifact(inputs_to_run)
            self._compare_outputs(
                reference_output, stage_output, quantization_scale, atol, rtol, qtol
            )

        return self

    @staticmethod
    def _assert_outputs_equal(model_output, ref_output, atol=1e-03, rtol=1e-03):
        """
        Helper testing function that asserts that the model output and the reference output
        are equal with some tolerance. Due to numerical differences between eager mode and
        the XNNPACK's backend, we relax the detal such that absolute tolerance is 1e-3. and
        relative tolerance is 1e-3. In the event that the computation was quantized, we
        further relax the tolerance to one quantized step (equal to the quantization scale).
        This allows the quantized value to differ by 1 between the reference and model output.
        """

        assert len(model_output) == len(ref_output)

        for i in range(len(model_output)):
            model = model_output[i]
            ref = ref_output[i]
            assert (
                ref.shape == model.shape
            ), f"Output {i} shape {model.shape} does not match reference output shape {ref.shape}"
            assert torch.allclose(
                model,
                ref,
                atol=atol,
                rtol=rtol,
            ), (
                f"Output {i} does not match reference output.\n"
                f"\tGiven atol: {atol}, rtol: {rtol}.\n"
                f"\tOutput tensor shape: {model.shape}, dtype: {model.dtype}\n"
                f"\tDifference: max: {torch.max(model-ref)}, abs: {torch.max(torch.abs(model-ref))}, mean abs error: {torch.mean(torch.abs(model-ref))}.\n"
                f"\t-- Model vs. Reference --\n"
                f"\t Numel: {model.numel()}, {ref.numel()}\n"
                f"\tMedian: {model.median()}, {ref.median()}\n"
                f"\t  Mean: {model.mean()}, {ref.mean()}\n"
                f"\t   Max: {model.max()}, {ref.max()}\n"
                f"\t   Min: {model.min()}, {ref.min()}\n"
            )

    @staticmethod
    def _compare_outputs(
        reference_output,
        stage_output,
        quantization_scale=None,
        atol=1e-03,
        rtol=1e-03,
        qtol=0,
    ):
        """
        Compares the original of the original nn module with the output of the generated artifact.
        This requres calling run_method before calling compare_outputs. As that runs the generated
        artifact on the sample inputs and sets the stage output to be compared against the reference.
        """
        # Wrap both outputs as tuple, since executor output is always a tuple even if single tensor
        if isinstance(reference_output, torch.Tensor):
            reference_output = (reference_output,)
        if isinstance(stage_output, torch.Tensor):
            stage_output = (stage_output,)

        # If a qtol is provided and we found an dequantization node prior to the output, relax the
        # atol by qtol quant units.
        if quantization_scale is not None:
            atol += quantization_scale * qtol

        Tester._assert_outputs_equal(
            stage_output,
            reference_output,
            atol=atol,
            rtol=rtol,
        )

    @staticmethod
    def _calculate_reference_output(
        program: ExportedProgram, inputs
    ) -> Tuple[torch.Tensor, Optional[float]]:
        """
        Execute the reference program and return the output. If the output comes from a dequantize node,
        return the quantization scale as well.
        """

        # Locate the output node.
        output_node = None
        for node in program.graph.nodes:
            if node.op == "output":
                output_node = node
                break
        assert output_node is not None

        # Look for a dequantization node in the output node args. Returned values are found in the first
        # argument of the output node.
        dequant_node = None
        for arg_node in output_node.args[0]:
            if (
                arg_node.op == "call_function"
                and arg_node.target
                == torch.ops.quantized_decomposed.dequantize_per_tensor.default
            ):
                dequant_node = arg_node
                break

        scale = None
        if dequant_node is not None:
            original_target = dequant_node.target

            # Replace the dequant node with shim to intercept the quantization parameters.
            # It will be invoked when we evaluate the program to find the reference outputs.
            def dequant_shim(*args):
                nonlocal scale
                scale = args[1]
                result = original_target(*args)
                return result

            dequant_node.target = dequant_shim

        output = program.module()(*inputs)
        return output, scale
