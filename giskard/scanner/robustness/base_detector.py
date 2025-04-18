from typing import Optional, Sequence

from abc import ABC, abstractmethod

import numpy as np
import pandas as pd

from ...datasets.base import Dataset
from ...llm import LLMImportError
from ...models.base import BaseModel
from ...models.base.model_prediction import ModelPredictionResults
from ..issues import Issue, IssueLevel, Robustness
from ..logger import logger
from ..registry import Detector
from .base_perturbation_function import PerturbationFunction
from .numerical_transformations import NumericalTransformation
from .text_transformations import TextTransformation


def _relative_delta(actual: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """
    Computes elementwise relative delta. If reference[i] == 0, we replace it with epsilon
    to avoid division by zero.
    """
    epsilon = 1e-9
    safe_ref = np.where(reference == 0, epsilon, reference)
    return (actual - reference) / safe_ref


def _get_default_num_samples(model) -> int:
    if model.is_text_generation:
        return 10
    return 1_000


def _get_default_output_sensitivity(model) -> float:
    if model.is_text_generation:
        return 0.15
    return 0.05


def _get_default_threshold(model) -> float:
    if model.is_text_generation:
        return 0.10
    return 0.05


def _generate_robustness_tests(issue: Issue):
    from ...testing.tests.metamorphic import test_metamorphic_invariance

    # Only generates a single metamorphic test
    return {
        f"Invariance to “{issue.transformation_fn}”": test_metamorphic_invariance(
            transformation_function=issue.transformation_fn,
            slicing_function=None,
            threshold=1 - issue.meta["threshold"],
            output_sensitivity=issue.meta.get("output_sentitivity", None),
        )
    }


class BasePerturbationDetector(Detector, ABC):
    """
    Common parent class for metamorphic perturbation detectors (both text and numerical).
    """

    _issue_group = Robustness
    _taxonomy = ["avid-effect:performance:P0201"]

    def __init__(
        self,
        transformations: Optional[Sequence[PerturbationFunction]] = None,
        threshold: Optional[float] = None,
        output_sensitivity: Optional[float] = None,
        num_samples: Optional[int] = None,
    ):
        """
        Creates a new instance of the detector.

        Parameters
        ----------
        transformations: Optional[Sequence[PerturbationFunction]]
            The transformations used in the metamorphic testing. See :ref:`transformation_functions` for details
            about the available transformations. If not provided, a default set of transformations will be used.
        threshold: Optional[float]
            The threshold for the fail rate, which is defined as the proportion of samples for which the model
            prediction has changed. If the fail rate is greater than the threshold, an issue is created.
            If not provided, a default threshold will be used.
        output_sensitivity: Optional[float]
            For regression models, the output sensitivity is the maximum relative change in the prediction that is
            considered acceptable. If the relative change is greater than the output sensitivity, an issue is created.
            This parameter is ignored for classification models. If not provided, a default output sensitivity will be
            used.
        num_samples: Optional[int]
            The maximum number of samples to use for the metamorphic testing. If not provided, a default number of
            samples will be used.
        """
        self.transformations = transformations
        self.threshold = threshold
        self.num_samples = num_samples
        self.output_sensitivity = output_sensitivity

    @abstractmethod
    def _select_features(self, dataset: Dataset, features: Sequence[str]) -> Sequence[str]:
        raise NotImplementedError

    @abstractmethod
    def _get_default_transformations(self) -> Sequence[PerturbationFunction]:
        raise NotImplementedError

    @abstractmethod
    def _supports_text_generation(self) -> bool:
        raise NotImplementedError

    def _compute_passed(
        self,
        model: BaseModel,
        original_pred: ModelPredictionResults,
        perturbed_pred: ModelPredictionResults,
        output_sensitivity: float,
    ) -> np.ndarray:
        if model.is_classification:
            return original_pred.raw_prediction == perturbed_pred.raw_prediction

        elif model.is_regression:
            rel_delta = _relative_delta(perturbed_pred.raw_prediction, original_pred.raw_prediction)
            return np.abs(rel_delta) < output_sensitivity

        elif model.is_text_generation:
            if not self._supports_text_generation():
                raise NotImplementedError("Text generation is not supported by this detector.")
            try:
                import evaluate
            except ImportError as err:
                raise LLMImportError() from err

            scorer = evaluate.load("bertscore")
            score = scorer.compute(
                predictions=perturbed_pred.prediction,
                references=original_pred.prediction,
                model_type="distilbert-base-multilingual-cased",
                idf=True,
            )
            return np.array(score["f1"]) > 1 - output_sensitivity

        else:
            raise NotImplementedError("Only classification, regression, or text generation models are supported.")

    def _create_examples(
        self,
        original_data: Dataset,
        original_pred: ModelPredictionResults,
        perturbed_data: Dataset,
        perturbed_pred: ModelPredictionResults,
        feature: str,
        passed: np.ndarray,
        model: BaseModel,
        transformation_fn,
    ) -> pd.DataFrame:
        examples = original_data.df.loc[~passed, [feature]].copy()
        examples[f"{transformation_fn.name}({feature})"] = perturbed_data.df.loc[~passed, feature]

        examples["Original prediction"] = original_pred.prediction[~passed]
        examples["Prediction after perturbation"] = perturbed_pred.prediction[~passed]

        if model.is_classification:
            examples["Original prediction"] = examples["Original prediction"].astype(str)
            examples["Prediction after perturbation"] = examples["Prediction after perturbation"].astype(str)

            ps_before = pd.Series(original_pred.probabilities[~passed], index=examples.index)
            ps_after = pd.Series(perturbed_pred.probabilities[~passed], index=examples.index)

            examples["Original prediction"] += ps_before.apply(lambda p: f" (p={p:.2f})")
            examples["Prediction after perturbation"] += ps_after.apply(lambda p: f" (p={p:.2f})")

        return examples

    def _detect_issues(
        self,
        model: BaseModel,
        dataset: Dataset,
        transformation,
        features: Sequence[str],
    ) -> Sequence[Issue]:
        # Fall back to defaults if not explicitly set
        num_samples = self.num_samples if self.num_samples is not None else _get_default_num_samples(model)
        threshold = self.threshold if self.threshold is not None else _get_default_threshold(model)
        output_sensitivity = (
            self.output_sensitivity if self.output_sensitivity is not None else _get_default_output_sensitivity(model)
        )

        issues = []
        for feature in features:
            # Build transformation function for this feature
            transformation_fn = transformation(column=feature)
            transformed = dataset.transform(transformation_fn)

            # Select only the records which were changed
            changed_idx = dataset.df.index[transformed.df[feature] != dataset.df[feature]]
            if changed_idx.empty:
                continue

            # Select a random subset of the changed records
            if len(changed_idx) > num_samples:
                rng = np.random.default_rng(747)
                changed_idx = changed_idx[rng.choice(len(changed_idx), num_samples, replace=False)]

            # Build original vs. perturbed datasets
            original_data = Dataset(
                dataset.df.loc[changed_idx],
                target=dataset.target,
                column_types=dataset.column_types,
                validation=False,
            )
            perturbed_data = Dataset(
                transformed.df.loc[changed_idx],
                target=dataset.target,
                column_types=dataset.column_types,
                validation=False,
            )

            # Calculate predictions
            original_pred = model.predict(original_data)
            perturbed_pred = model.predict(perturbed_data)

            passed = self._compute_passed(
                model=model,
                original_pred=original_pred,
                perturbed_pred=perturbed_pred,
                output_sensitivity=output_sensitivity,
            )

            pass_rate = passed.mean()
            fail_rate = 1 - pass_rate
            logger.info(
                f"{self.__class__.__name__}: Testing `{feature}` for perturbation `{transformation.name}`\tFail rate: {fail_rate:.3f}"
            )

            if fail_rate >= threshold:
                # Severity
                issue_level = IssueLevel.MAJOR if fail_rate >= 2 * threshold else IssueLevel.MEDIUM

                # Description
                desc = (
                    "When feature “{feature}” is perturbed with the transformation “{transformation_fn}”, "
                    "the model changes its prediction in {fail_rate_percent}% of the cases. "
                    "We expected the predictions not to be affected by this transformation."
                )

                failed_size = (~passed).sum()
                slice_size = len(passed)

                issue = Issue(
                    model,
                    dataset,
                    group=self._issue_group,
                    level=issue_level,
                    transformation_fn=transformation_fn,
                    description=desc,
                    features=[feature],
                    meta={
                        "feature": feature,
                        "domain": f"Feature `{feature}`",
                        "deviation": f"{failed_size}/{slice_size} tested samples ({round(fail_rate * 100, 2)}%) changed prediction after perturbation",
                        "failed_size": failed_size,
                        "slice_size": slice_size,
                        "fail_rate": fail_rate,
                        "fail_rate_percent": round(fail_rate * 100, 2),
                        "metric": "Fail rate",
                        "metric_value": fail_rate,
                        "threshold": threshold,
                        "output_sentitivity": output_sensitivity,
                        "perturbed_data_slice": perturbed_data,
                        "perturbed_data_slice_predictions": perturbed_pred,
                    },
                    importance=fail_rate,
                    tests=_generate_robustness_tests,
                    taxonomy=self._taxonomy,
                    detector_name=self.__class__.__name__,
                )

                # Add examples
                examples = self._create_examples(
                    original_data,
                    original_pred,
                    perturbed_data,
                    perturbed_pred,
                    feature,
                    passed,
                    model,
                    transformation_fn,
                )
                issue.add_examples(examples)

                issues.append(issue)

        return issues

    def run(self, model: BaseModel, dataset: Dataset, features: Sequence[str]) -> Sequence[Issue]:
        """
        Runs the perturbation detector on the given model and dataset.

        Parameters
        ----------
        model: BaseModel
            The model to test.
        dataset: Dataset
            The dataset to use for testing.
        features: Sequence[str]
            The features (columns) to test.

        Returns
        -------
        Sequence[Issue]
            A list of issues found during the testing.
        """
        transformations = self.transformations or self._get_default_transformations()
        selected_features = self._select_features(dataset, features)

        logger.info(
            f"{self.__class__.__name__}: Running with transformations={[t.name for t in transformations]} "
            f"threshold={self.threshold} output_sensitivity={self.output_sensitivity} num_samples={self.num_samples}"
        )

        issues = []
        for transformation in transformations:
            issues.extend(self._detect_issues(model, dataset, transformation, selected_features))

        return [i for i in issues if i is not None]


class BaseTextPerturbationDetector(BasePerturbationDetector):
    """
    Base class for metamorphic detectors based on text transformations.
    """

    def _select_features(self, dataset: Dataset, features: Sequence[str]) -> Sequence[str]:
        # Only analyze text features
        return [
            f
            for f in features
            if dataset.column_types[f] == "text" and pd.api.types.is_string_dtype(dataset.df[f].dtype)
        ]

    @abstractmethod
    def _get_default_transformations(self) -> Sequence[TextTransformation]:
        raise NotImplementedError

    def _supports_text_generation(self) -> bool:
        return True


class BaseNumericalPerturbationDetector(BasePerturbationDetector):
    """
    Base class for metamorphic detectors based on numerical feature perturbations.
    """

    def _select_features(self, dataset: Dataset, features: Sequence[str]) -> Sequence[str]:
        # Only analyze numeric features
        return [f for f in features if dataset.column_types[f] == "numeric"]

    @abstractmethod
    def _get_default_transformations(self) -> Sequence[NumericalTransformation]:
        raise NotImplementedError

    def _supports_text_generation(self) -> bool:
        return False
