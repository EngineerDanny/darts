"""
Shap-based ForecastingModelExplainer
------------------------------
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
from IPython.core.display import display
from numpy import integer
from sklearn.multioutput import MultiOutputRegressor

from darts import TimeSeries
from darts.explainability.explainability import ForecastingModelExplainer
from darts.logging import get_logger, raise_if, raise_log
from darts.models.forecasting.forecasting_model import ForecastingModel
from darts.models.forecasting.regression_model import RegressionModel
from darts.utils.data.tabularization import create_lagged_data

logger = get_logger(__name__)


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
        model: ForecastingModel,
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
        - A background time series is a time series with which we 'train' the Explainer model.
        - A foreground time series is the time series we will explain according to the fitted Explainer model.

        Parameters
        ----------
        model
            A ForecastingModel we want to explain. It has to be fitted first.
        background_series
            Optionally, a TimeSeries or a list of time series we want to use to 'train' with any foreground we want
            to explain.
            This is optional, for 2 reasons:
                - In general we want to keep the training_series of the model and this is the default one,
                but in case of multiple time series training (global or meta learning) the ForecastingModel doesn't
                save them. In this case we need to feed a background time series.
                - We might want to consider a reduced well chosen background in order to reduce computation
                time.
        background_past_covariates
            Optionally, a past covariates TimeSeries or list of TimeSeries if the model needs it.
        background_future_covariates
            Optionally, a future covariates TimeSeries or list of TimeSeries if the model needs it.
        background_nb_samples
            Optionally, sampling a subset of the original background (generally to compute faster, especially
            if shap methods is kernel or permutation)
        shap_method
            Optionally, a shap method we want to apply. By default, the method is chosen automatically with an
            internal mapping.
            Supported values : “permutation”, “partition”, “tree”, “kernel”, “sampling”, “linear”, “deep”,
            “gradient”, "additive"
        **kwargs
            Optionally, an additional keyword arguments passed to the shap_method chosen, if any.
        """
        if not issubclass(type(model), RegressionModel):
            raise_log(
                ValueError(
                    "Invalid model type. For now, only RegressionModel type can be explained."
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
                        "Invalid shap method. Please choose one value among the following: [partition, tree, "
                        "kernel, sampling, linear, deep, gradient, additive]."
                    )
                )
        else:
            self.shap_method = None

        self.explainers = _RegressionShapExplainers(
            self,
            background_nb_samples,
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
    ) -> Union[
        Dict[integer, Dict[str, TimeSeries]],
        Sequence[Dict[integer, Dict[str, TimeSeries]]],
    ]:
        super().explain(
            foreground_series, foreground_past_covariates, foreground_future_covariates
        )

        if foreground_series is None:
            foreground_series = self.background_series
            foreground_past_covariates = self.background_past_covariates
            foreground_future_covariates = self.background_future_covariates

        if isinstance(foreground_series, TimeSeries):
            foreground_series = [foreground_series]
            foreground_past_covariates = (
                [foreground_past_covariates] if foreground_past_covariates else None
            )
            foreground_future_covariates = (
                [foreground_future_covariates] if foreground_future_covariates else None
            )

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
            for h in range(self.n):
                tmp = {}
                for idx, t in enumerate(self.target_names):

                    tmp[t] = TimeSeries.from_times_and_values(
                        shap_[h][t].time_index,
                        shap_[h][t].values,
                        columns=shap_[h][t].feature_names,
                    )
                shap_values_dict[h] = tmp

            shap_values_list.append(shap_values_dict)

        if len(shap_values_list) == 1:
            shap_values_list = shap_values_list[0]

        return shap_values_list

    def summary_plot(
        self,
        horizons: Optional[Sequence[int]] = None,
        target_names: Optional[Sequence[str]] = None,
        nb_samples: Optional[int] = None,
        plot_type: Optional[str] = "dot",
    ):
        """
        Display a shap plot summary per (target, horizon)).
        We here reuse the initial background data as foreground (potentially sampled) to give a general importance
        plot for each feature.
        If no target names and/or no horizons are provided, we plot all summary plots.

        Parameters
        ----------
        horizons
            Optionally, a list of integer values representing which elements in the future
            we want to explain, starting from the first timestamp prediction at 0.
            For now we consider only models with output_chunk_length and it can't be bigger than output_chunk_length.
        target_names
            Optionally, A list of string naming the target names we want to plot.
        nb_samples
            Optionally, an integer value sampling the foreground series (based on the backgound),
            for the sake of performance.
        plot_type
            Optionally, string value for the type of plot proposed by shap library. Currently,
            the following are available: 'dot', 'bar', 'violin'.

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
                    shaps_[h][t], foreground_X_sampled, plot_type=plot_type
                )

    def force_plot_from_ts(
        self,
        foreground_series: TimeSeries,
        foreground_past_covariates: Optional[TimeSeries] = None,
        foreground_future_covariates: Optional[TimeSeries] = None,
        horizons: Optional[Sequence[int]] = None,
        target_names: Optional[Sequence[str]] = None,
    ):
        """
        Display a shap force_plot per target and per horizon.
        Here the inputs are the foreground series (and not the shap explanations objects)
        If no target names and/or no horizons are provided, we plot all force_plots.
        'original sample ordering' has to be selected to observe the time series chronologically.

        Parameters
        ----------
        foreground_series
            Optionally, target timeseries we want to explain. Can be multivariate.
            If none is provided, explain will automatically provide the whole background TimeSeries explanation.
        foreground_past_covariates
            Optionally, past covariate timeseries if needed by model.
        foreground_future_covariates
            Optionally, future covariate timeseries if needed by model.
            Optionally, A list of string naming the target names we want to plot.
        horizons
            Optionally, a list of integer values representing which elements in the future
            we want to explain, starting from the first timestamp prediction at 0.
            For now we consider only models with output_chunk_length and it can't be bigger than output_chunk_length.
        target_names
            Optionally, A list of string naming the target names we want to plot.

        """
        horizons, target_names = self._check_horizons_and_targets(
            horizons, target_names
        )

        foreground_X = self.explainers._create_regression_model_shap_X(
            foreground_series, foreground_past_covariates, foreground_future_covariates
        )

        shap_ = self.explainers.shap_explanations(foreground_X, horizons, target_names)
        for t in target_names:
            for h in horizons:
                print("Target: `{}` - Horizon: {}".format(t, "t+" + str(h)))
                display(
                    shap.force_plot(
                        base_value=shap_[h][t],
                        features=foreground_X,
                        out_names=t,
                    )
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
        shap_explainer: ShapExplainer,
        background_nb_samples: Optional[int] = None,
        **kwargs,
    ):

        self.model = ShapExplainer.model
        self.target_dim = self.model.input_dim["target"]

        self.is_multiOutputRegressor = isinstance(
            self.model.model, MultiOutputRegressor
        )

        self.target_names = ShapExplainer.target_names
        self.past_covariates_names = ShapExplainer.past_covariates_names
        self.future_covariates_names = ShapExplainer.future_covariates_names

        self.n = ShapExplainer.n
        self.shap_method = ShapExplainer.shap_method
        self.background_series = ShapExplainer.background_series
        self.background_past_covariates = ShapExplainer.background_past_covariates
        self.background_future_covariates = ShapExplainer.background_future_covariates

        self.single_output = False
        if (self.n == 1) and (self.target_dim) == 1:
            self.single_output = True

        self.background_X = self._create_regression_model_shap_X(
            self.background_series,
            self.background_past_covariates,
            self.background_future_covariates,
            background_nb_samples,
        )

        if self.is_multiOutputRegressor:
            self.explainers = {}
            for i in range(self.n):
                self.explainers[i] = {}
                for j in range(self.target_dim):
                    self.explainers[i][j] = self._get_explainer_sklearn(
                        self.model.model.estimators_[i + j],
                        self.background_X,
                        self.shap_method,
                        **kwargs,
                    )
        else:
            self.explainers = self._get_explainer_sklearn(
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
            Optionally, a list of integer values representing which elements in the future
            we want to explain, starting from the first timestamp prediction at 0.
            For now we consider only models with output_chunk_length and it can't be bigger than output_chunk_length.
        target_names
            Optionally, A list of string naming the target names we want to explain.

        """

        # Creation of an unified dictionary between multiOutputRegressor estimators and
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
                    if self.single_output is False:
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

    def _get_explainer_sklearn(
        self,
        model_sklearn,
        background_X: pd.DataFrame,
        shap_method: Optional[ShapMethod] = None,
        **kwargs,
    ):

        model_name = type(model_sklearn).__name__

        if shap_method is None:
            if model_name in self.default_sklearn_shap_explainers.keys():
                shap_method = self.default_sklearn_shap_explainers[model_name]
                print(shap_method)
            else:
                shap_method = _ShapMethod.KERNEL

        if shap_method == _ShapMethod.TREE:
            if "feature_perturbation" in kwargs:
                if kwargs.get("feature_perturbation") == "interventional":
                    explainer = shap.TreeExplainer(
                        model_sklearn, background_X, **kwargs
                    )
                else:
                    explainer = shap.TreeExplainer(model_sklearn, **kwargs)
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
            print(kwargs)
            explainer = shap.LinearExplainer(model_sklearn, background_X, **kwargs)
        elif shap_method == _ShapMethod.DEEP:
            explainer = shap.LinearExplainer(model_sklearn, background_X, **kwargs)
        elif shap_method == _ShapMethod.ADDITIVE:
            explainer = shap.AdditiveExplainer(model_sklearn, background_X, **kwargs)

        logger.info("The shap method used is of type: " + str(type(explainer)))

        return explainer

    def _create_regression_model_shap_X(
        self, target_series, past_covariates, future_covariates, n_samples=None
    ):

        lags_list = self.model.lags.get("target")
        lags_past_covariates_list = self.model.lags.get("past")
        lags_future_covariates_list = self.model.lags.get("future")

        X, _ = create_lagged_data(
            target_series,
            self.n,
            past_covariates,
            future_covariates,
            lags_list,
            lags_past_covariates_list,
            lags_future_covariates_list,
        )

        X = pd.DataFrame(X)

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
