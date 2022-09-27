"""
Shap-based ForecastingModelExplainer
------------------------------------
This class is meant to wrap a shap explainer (https://github.com/slundberg/shap) specifically for time series.

Warning

This is only a shap value of direct influence and doesn't take into account relationships
between past lags themselves. Hence a given past lag could also have an indirect influence via the
intermediate past lags elements between it and the time step we want to explain, if we assume that
the intermediate past lags are generated by the same model.

TODO
    - Optional De-trend  if the timeseries is not stationary.
    There would be 1) a stationarity test and 2) a de-trend methodology for the target. It can be for
    example target - moving_average(input_chunk_length).

"""

from enum import Enum
from typing import Dict, NewType, Optional, Sequence, Tuple, Union

import matplotlib.pyplot as plt
import pandas as pd
import shap
from numpy import integer
from sklearn.multioutput import MultiOutputRegressor

from darts import TimeSeries
from darts.explainability.explainability import (
    ExplainabilityResult,
    ForecastingModelExplainer,
)
from darts.logging import get_logger, raise_if, raise_log
from darts.models.forecasting.regression_model import RegressionModel
from darts.utils.data.tabularization import create_lagged_data

logger = get_logger(__name__)

MIN_BACKGROUND_SAMPLE = 10


class _ShapMethod(Enum):
    TREE = 0
    GRADIENT = 1
    DEEP = 2
    KERNEL = 3
    SAMPLING = 4
    PARTITION = 5
    LINEAR = 6
    PERMUTATION = 7
    ADDITIVE = 8


ShapMethod = NewType("ShapMethod", _ShapMethod)


class ShapExplainer(ForecastingModelExplainer):
    def __init__(
        self,
        model: RegressionModel,
        background_series: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        background_past_covariates: Optional[
            Union[TimeSeries, Sequence[TimeSeries]]
        ] = None,
        background_future_covariates: Optional[
            Union[TimeSeries, Sequence[TimeSeries]]
        ] = None,
        background_nb_samples: Optional[int] = None,
        shap_method: Optional[str] = None,
        **kwargs,
    ):
        """ShapExplainer

        Naming:
        - A background series is a `TimeSeries` with which we 'train' the `Explainer` model.
        - A foreground series is the `TimeSeries` we will explain according to the fitted `Explainer` model.

        Currently, ShapExplainer only works with `RegressionModel` forecasting models.
        The number of explained horizons (t, t+1, ...) will be equal to `output_chunk_length` of `model`.

        Parameters
        ----------
        model
            A `ForecastingModel` we want to explain. It must be fitted first.
        background_series
            A series or list of series to *train* the `ForecastingModelExplainer` along with any foreground series.
            Consider using a reduced well-chosen backgroundto to reduce computation time.
                - optional if `model` was fit on a single target series. By default, it is the `series` used
                at fitting time.
                - mandatory if `model` was fit on multiple (list of) target series.
        background_past_covariates
            A past covariates series or list of series that the model needs once fitted.
        background_future_covariates
            A future covariates series or list of series that the model needs once fitted.
        background_nb_samples
            Optionally, whether to sample a subset of the original background. Randomly picks
            `background_nb_samples` training samples of the constructed training dataset (using `shap.utils.sample()`).
            Generally for faster computation, especially when `shap_method` is ``"kernel"`` or ``"permutation"``.
        shap_method
            Optionally, the shap method we want to apply. By default, the method is chosen automatically with an
            internal mapping. Supported values : ``"permutation", "partition", "tree", "kernel", "sampling", "linear",
            "deep", "gradient", "additive"``.
        **kwargs
            Optionally, additional keyword arguments passed to `shap_method`.
        Examples
        --------
        >>> from darts.explainability.shap_explainer import ShapExplainer
        >>> from darts.models import LinearRegressionModel
        >>> series = AirPassengersDataset().load()
        >>> model = LinearRegressionModel(lags=12)
        >>> model.fit(series[:-36])
        >>> shap_explain = ShapExplainer(model)
        >>> shap_explain.summary_plot()
        """
        if not issubclass(type(model), RegressionModel):
            raise_log(
                ValueError(
                    "Invalid `model` type. Currently, only models of type `RegressionModel` are supported."
                ),
                logger,
            )

        super().__init__(
            model,
            background_series,
            background_past_covariates,
            background_future_covariates,
        )

        # As we only use RegressionModel, we fix the forecast n step ahead we want to explain as
        # output_chunk_length
        self.n = self.model.output_chunk_length

        if shap_method is not None:
            shap_method = shap_method.upper()
            if shap_method in _ShapMethod.__members__:
                self.shap_method = _ShapMethod[shap_method]
            else:
                raise_log(
                    ValueError(
                        "Invalid `shap_method`. Please choose one value among the following: ['partition', 'tree', "
                        "'kernel', 'sampling', 'linear', 'deep', 'gradient', 'additive']."
                    )
                )
        else:
            self.shap_method = None

        self.explainers = _RegressionShapExplainers(
            model=self.model,
            n=self.n,
            target_names=self.target_names,
            past_covariates_names=self.past_covariates_names,
            future_covariates_names=self.future_covariates_names,
            background_series=self.background_series,
            background_past_covariates=self.background_past_covariates,
            background_future_covariates=self.background_future_covariates,
            shap_method=self.shap_method,
            background_nb_samples=background_nb_samples,
            **kwargs,
        )

    def explain(
        self,
        foreground_series: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        foreground_past_covariates: Optional[
            Union[TimeSeries, Sequence[TimeSeries]]
        ] = None,
        foreground_future_covariates: Optional[
            Union[TimeSeries, Sequence[TimeSeries]]
        ] = None,
        horizons: Optional[Sequence[int]] = None,
        target_names: Optional[Sequence[str]] = None,
    ) -> ExplainabilityResult:
        super().explain(
            foreground_series, foreground_past_covariates, foreground_future_covariates
        )

        if foreground_series is None:
            foreground_series = self.background_series
            foreground_past_covariates = self.background_past_covariates
            foreground_future_covariates = self.background_future_covariates
        else:
            if self.model.encoders.encoding_available:
                (
                    foreground_past_covariates,
                    foreground_future_covariates,
                ) = self.model.encoders.encode_train(
                    target=foreground_series,
                    past_covariate=foreground_past_covariates,
                    future_covariate=foreground_future_covariates,
                )

        # ensure list of TimeSeries format
        def to_list(s):
            return [s] if isinstance(s, TimeSeries) and s is not None else s

        foreground_series = to_list(foreground_series)
        foreground_past_covariates = to_list(foreground_past_covariates)
        foreground_future_covariates = to_list(foreground_future_covariates)

        horizons, target_names = self._check_horizons_and_targets(
            horizons, target_names
        )

        shap_values_list = []

        for idx, foreground_ts in enumerate(foreground_series):

            foreground_X = self.explainers._create_regression_model_shap_X(
                foreground_ts,
                foreground_past_covariates[idx],
                foreground_future_covariates[idx],
            )

            shap_ = self.explainers.shap_explanations(
                foreground_X, horizons, target_names
            )

            shap_values_dict = {}
            for h in horizons:
                tmp = {}
                for t in target_names:
                    tmp[t] = TimeSeries.from_times_and_values(
                        shap_[h][t].time_index,
                        shap_[h][t].values,
                        columns=shap_[h][t].feature_names,
                    )
                shap_values_dict[h] = tmp

            shap_values_list.append(shap_values_dict)

        if len(shap_values_list) == 1:
            shap_values_list = shap_values_list[0]

        return ExplainabilityResult(shap_values_list)

    def summary_plot(
        self,
        horizons: Optional[Sequence[int]] = None,
        target_names: Optional[Sequence[str]] = None,
        nb_samples: Optional[int] = None,
        plot_type: Optional[str] = "dot",
        **kwargs,
    ):
        """
        Display a shap plot summary per (target, horizon)).
        We here reuse the initial background data as foreground (potentially sampled) to give a general importance
        plot for each feature.
        If no target names and/or no horizons are provided, we plot all summary plots.

        Parameters
        ----------
        horizons
            Optionally, a list of integers representing which points/steps in the future we want to explain,
            starting from the first prediction step at 0. Currently, only forecasting models are supported which
            provide an `output_chunk_length` parameter. `horizons` must not be larger than `output_chunk_length`.
        target_names
            Optionally, a list of strings with the target components we want to explain.
        nb_samples
            Optionally, an integer for sampling the foreground series (based on the backgound),
            for the sake of performance.
        plot_type
            Optionally, specify which of the propres shap library plot type to use. Can be one of
            ``'dot', 'bar', 'violin'``.

        """

        horizons, target_names = self._check_horizons_and_targets(
            horizons, target_names
        )

        if nb_samples:
            foreground_X_sampled = shap.utils.sample(
                self.explainers.background_X, nb_samples
            )
        else:
            foreground_X_sampled = self.explainers.background_X

        shaps_ = self.explainers.shap_explanations(
            foreground_X_sampled, horizons, target_names
        )

        for t in target_names:
            for h in horizons:
                plt.title("Target: `{}` - Horizon: {}".format(t, "t+" + str(h)))
                shap.summary_plot(
                    shaps_[h][t],
                    foreground_X_sampled,
                    plot_type=plot_type,
                    **kwargs,
                )

    def force_plot_from_ts(
        self,
        horizon: int = None,
        target_name: str = None,
        foreground_series: TimeSeries = None,
        foreground_past_covariates: Optional[TimeSeries] = None,
        foreground_future_covariates: Optional[TimeSeries] = None,
        **kwargs,
    ):
        """
        Display a shap force_plot per target and per horizon.
        For each target and horizon, it displays SHAP values of each lag/covariate with an additive
        force layout.
        Here the inputs are the foreground series (and not the shap explanations objects)
        If no target names and/or no horizons are provided, we plot all force_plots.
        'original sample ordering' has to be selected to observe the time series chronologically.

        Parameters
        ----------
        horizon
            An integer for the point/step in the future we want to explain, starting from the first
            prediction step at 0. Currently, only forecasting models are supported which provide an
            `output_chunk_length` parameter. `horizons` must not be larger than `output_chunk_length`.
        target_name
            The target component name to plot.
        foreground_series
            The target series to explain. Can be multivariate.
        foreground_past_covariates
            Optionally, a past covariate series if required by the forecasting model.
        foreground_future_covariates
            Optionally, a future covariate series if required by the forecasting model.
        **kwargs
            Optionally, additional keyword arguments passed to `shap.force_plot()`.
        """

        if self.model.encoders.encoding_available:
            (
                foreground_past_covariates,
                foreground_future_covariates,
            ) = self.model.encoders.encode_train(
                target=foreground_series,
                past_covariate=foreground_past_covariates,
                future_covariate=foreground_future_covariates,
            )

        foreground_X = self.explainers._create_regression_model_shap_X(
            foreground_series, foreground_past_covariates, foreground_future_covariates
        )

        shap_ = self.explainers.shap_explanations(
            foreground_X, [horizon], [target_name]
        )

        print("Target: `{}` - Horizon: {}".format(target_name, "t+" + str(horizon)))
        return shap.force_plot(
            base_value=shap_[horizon][target_name],
            features=foreground_X,
            out_names=target_name,
            **kwargs,
        )

    def _check_horizons_and_targets(self, horizons, target_names) -> Tuple[int, str]:
        if target_names is not None:
            raise_if(
                any(
                    [
                        target_name not in self.target_names
                        for target_name in target_names
                    ]
                ),
                "One of the target names doesn't exist. Please review your target_names input",
            )
        else:
            target_names = self.target_names

        if horizons is not None:
            raise_if(
                any([max(horizons) > self.n - 1, min(horizons) < 0]),
                "One of the horizons is too large. Please review your horizons input.",
            )
        else:
            horizons = range(self.n)

        return horizons, target_names


class _RegressionShapExplainers:
    """
    Helper Class to wrap the different cases we encounter with shap different explainers, multivariates,
    horizon etc.
    Aim to provide shap values for any type of RegressionModel. Manage the MultioutputRegressor cases.
    For darts RegressionModel only.
    """

    default_sklearn_shap_explainers = {
        # Gradient boosting models
        "LGBMRegressor": _ShapMethod.TREE,
        "CatBoostRegressor": _ShapMethod.TREE,
        "XGBRegressor": _ShapMethod.TREE,
        "GradientBoostingRegressor": _ShapMethod.TREE,
        # Tree models
        "DecisionTreeRegressor": _ShapMethod.TREE,
        "ExtraTreeRegressor": _ShapMethod.TREE,
        # Ensemble model
        "AdaBoostRegressor": _ShapMethod.PERMUTATION,
        "BaggingRegressor": _ShapMethod.PERMUTATION,
        "ExtraTreesRegressor": _ShapMethod.PERMUTATION,
        "HistGradientBoostingRegressor": _ShapMethod.PERMUTATION,
        "RandomForestRegressor": _ShapMethod.PERMUTATION,
        "RidgeCV": _ShapMethod.PERMUTATION,
        "Ridge": _ShapMethod.PERMUTATION,
        # Linear models
        "LinearRegression": _ShapMethod.LINEAR,
        "ARDRegression": _ShapMethod.LINEAR,
        "MultiTaskElasticNet": _ShapMethod.LINEAR,
        "MultiTaskElasticNetCV": _ShapMethod.LINEAR,
        "MultiTaskLasso": _ShapMethod.LINEAR,
        "MultiTaskLassoCV": _ShapMethod.LINEAR,
        "PassiveAggressiveRegressor": _ShapMethod.LINEAR,
        "PoissonRegressor": _ShapMethod.LINEAR,
        "QuantileRegressor": _ShapMethod.LINEAR,
        "RANSACRegressor": _ShapMethod.LINEAR,
        "GammaRegressor": _ShapMethod.LINEAR,
        "HuberRegressor": _ShapMethod.LINEAR,
        "BayesianRidge": _ShapMethod.LINEAR,
        "SGDRegressor": _ShapMethod.LINEAR,
        "TheilSenRegressor": _ShapMethod.LINEAR,
        "TweedieRegressor": _ShapMethod.LINEAR,
        # Gaussian process
        "GaussianProcessRegressor": _ShapMethod.PERMUTATION,
        # neighbors
        "KNeighborsRegressor": _ShapMethod.PERMUTATION,
        "RadiusNeighborsRegressor": _ShapMethod.PERMUTATION,
        # Neural network
        "MLPRegressor": _ShapMethod.PERMUTATION,
    }

    def __init__(
        self,
        model: RegressionModel,
        n: int,
        target_names: Sequence[str],
        past_covariates_names: Sequence[str],
        future_covariates_names: Sequence[str],
        background_series: Sequence[TimeSeries],
        background_past_covariates: Sequence[TimeSeries],
        background_future_covariates: Sequence[TimeSeries],
        shap_method: ShapMethod,
        background_nb_samples: Optional[int] = None,
        **kwargs,
    ):

        self.model = model
        self.target_dim = self.model.input_dim["target"]
        self.is_multiOutputRegressor = isinstance(
            self.model.model, MultiOutputRegressor
        )

        self.target_names = target_names
        self.past_covariates_names = past_covariates_names
        self.future_covariates_names = future_covariates_names

        self.n = n
        self.shap_method = shap_method
        self.background_series = background_series
        self.background_past_covariates = background_past_covariates
        self.background_future_covariates = background_future_covariates

        self.single_output = False
        if self.n == 1 and self.target_dim == 1:
            self.single_output = True

        self.background_X = self._create_regression_model_shap_X(
            self.background_series,
            self.background_past_covariates,
            self.background_future_covariates,
            background_nb_samples,
            train=True,
        )

        if self.is_multiOutputRegressor:
            self.explainers = {}
            for i in range(self.n):
                self.explainers[i] = {}
                for j in range(self.target_dim):
                    self.explainers[i][j] = self._build_explainer_sklearn(
                        self.model.get_multioutput_estimator(horizon=i, target_dim=j),
                        self.background_X,
                        self.shap_method,
                        **kwargs,
                    )
        else:
            self.explainers = self._build_explainer_sklearn(
                self.model.model, self.background_X, self.shap_method, **kwargs
            )

    def shap_explanations(
        self,
        foreground_X,
        horizons: Optional[Sequence[int]] = None,
        target_names: Optional[Sequence[str]] = None,
    ) -> Dict[integer, Dict[str, shap.Explanation]]:

        """
        Return a dictionary of dictionaries of shap.Explanation instances:
        - the first dimension corresponds to the n forecasts ahead we want to explain (Horizon).
        - the second dimension corresponds to each component of the target time series.
        Parameters
        ----------
        foreground_X
            the Dataframe of lags features specific of darts RegressionModel.
        horizons
            Optionally, a list of integers representing which points/steps in the future we want to explain,
            starting from the first prediction step at 0. Currently, only forecasting models are supported which
            provide an `output_chunk_length` parameter. `horizons` must not be larger than `output_chunk_length`.
        target_names
            Optionally, a list of strings with the target components we want to explain.

        """

        # create a unified dictionary between multiOutputRegressor estimators and
        # native multiOutput estimators
        shap_explanations = {}
        if self.is_multiOutputRegressor:

            for h in horizons:
                tmp_n = {}
                for t_idx, t in enumerate(target_names):
                    explainer = self.explainers[h][t_idx](foreground_X)
                    explainer.base_values = explainer.base_values.ravel()
                    explainer.time_index = foreground_X.index
                    tmp_n[t] = explainer
                shap_explanations[h] = tmp_n
        else:
            # the native multioutput forces us to recompute all horizons and targets
            shap_explanation_tmp = self.explainers(foreground_X)
            for h in horizons:
                tmp_n = {}
                for t_idx, t in enumerate(target_names):
                    if not self.single_output:
                        tmp_t = shap.Explanation(
                            shap_explanation_tmp.values[
                                :, :, self.target_dim * h + t_idx
                            ]
                        )
                        tmp_t.base_values = shap_explanation_tmp.base_values[
                            :, self.target_dim * h + t_idx
                        ].ravel()
                    else:
                        tmp_t = shap_explanation_tmp
                        tmp_t.base_values = shap_explanation_tmp.base_values.ravel()

                    tmp_t.feature_names = shap_explanation_tmp.feature_names
                    tmp_t.time_index = foreground_X.index
                    tmp_n[t] = tmp_t
                shap_explanations[h] = tmp_n

        return shap_explanations

    def _build_explainer_sklearn(
        self,
        model_sklearn,
        background_X: pd.DataFrame,
        shap_method: Optional[ShapMethod] = None,
        **kwargs,
    ):

        model_name = type(model_sklearn).__name__

        if shap_method is None:
            if model_name in self.default_sklearn_shap_explainers:
                shap_method = self.default_sklearn_shap_explainers[model_name]
            else:
                shap_method = _ShapMethod.KERNEL

        if shap_method == _ShapMethod.TREE:
            if kwargs.get("feature_perturbation") == "interventional":
                explainer = shap.TreeExplainer(model_sklearn, background_X, **kwargs)
            else:
                explainer = shap.TreeExplainer(model_sklearn, **kwargs)
        elif shap_method == _ShapMethod.PERMUTATION:
            explainer = shap.PermutationExplainer(
                model_sklearn.predict, background_X, **kwargs
            )
        elif shap_method == _ShapMethod.PARTITION:
            explainer = shap.PermutationExplainer(
                model_sklearn.predict, background_X, **kwargs
            )
        elif shap_method == _ShapMethod.KERNEL:
            explainer = shap.KernelExplainer(
                model_sklearn.predict, background_X, keep_index=True, **kwargs
            )
        elif shap_method == _ShapMethod.LINEAR:
            explainer = shap.LinearExplainer(model_sklearn, background_X, **kwargs)
        elif shap_method == _ShapMethod.DEEP:
            explainer = shap.LinearExplainer(model_sklearn, background_X, **kwargs)
        elif shap_method == _ShapMethod.ADDITIVE:
            explainer = shap.AdditiveExplainer(model_sklearn, background_X, **kwargs)
        else:
            raise ValueError(
                "shap_method must be one of the following: "
                + ", ".join([e.value for e in _ShapMethod])
            )

        logger.info("The shap method used is of type: " + str(type(explainer)))

        return explainer

    def _create_regression_model_shap_X(
        self,
        target_series,
        past_covariates,
        future_covariates,
        n_samples=None,
        train=False,
    ) -> pd.DataFrame:
        """
        Creates the shap format input for regression models.
        The output is a pandas DataFrame representing all lags of different covariates, and with adequate
        column names in order to map feature / shap values.
        It uses create_lagged_data also used in RegressionModel to build the tabular dataset.

        """

        lags_list = self.model.lags.get("target")
        lags_past_covariates_list = self.model.lags.get("past")
        lags_future_covariates_list = self.model.lags.get("future")

        X, _, indexes = create_lagged_data(
            target_series,
            self.n,
            past_covariates,
            future_covariates,
            lags_list,
            lags_past_covariates_list,
            lags_future_covariates_list,
        )

        if train:
            X = pd.DataFrame(X)
            if len(X) <= MIN_BACKGROUND_SAMPLE:
                raise_log(
                    ValueError(
                        "The number of samples in the background dataset is too small to compute shap values."
                    )
                )
        else:
            X = pd.DataFrame(X, index=indexes[0])

        if n_samples:
            X = shap.utils.sample(X, n_samples)

        # We keep the creation order of the different lags/features in create_lagged_data
        lags_names_list = []
        if lags_list:
            for lag in lags_list:
                for t_name in self.target_names:
                    lags_names_list.append(t_name + "_target_lag" + str(lag))
        if lags_past_covariates_list:
            for lag in lags_past_covariates_list:
                for t_name in self.past_covariates_names:
                    lags_names_list.append(t_name + "_past_cov_lag" + str(lag))
        if lags_future_covariates_list:
            for lag in lags_future_covariates_list:
                for t_name in self.future_covariates_names:
                    lags_names_list.append(t_name + "_fut_cov_lag" + str(lag))

        X = X.rename(
            columns={
                name: lags_names_list[idx]
                for idx, name in enumerate(X.columns.to_list())
            }
        )

        return X
