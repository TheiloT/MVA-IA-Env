import json
import os
import time

import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
from darts import TimeSeries
from darts.dataprocessing.transformers import Scaler
from darts.metrics import mae
from darts.metrics import mape
from darts.metrics import rmse
from darts.metrics import r2_score
from darts.models import LinearRegressionModel, AutoARIMA, RandomForest, LightGBMModel, Prophet
from waste_prediction.params import RESULTS_PATH, PROCESSED_DATA_PASS



def _compute_non_conformity_scores(fun, pred_series_list, actual_series):
    nc_scores = []
    conformal_prediction_horizon = len(pred_series_list[0])
    for i, pred_series in enumerate(pred_series_list):
        # print(pred_series.pd_dataframe())
        # print(actual_series[i*conformal_prediction_horizon:(i+1)*conformal_prediction_horizon].pd_dataframe())
        nc_scores.append(fun(pred_series, actual_series[i*conformal_prediction_horizon:(i+1)*conformal_prediction_horizon]))
    return nc_scores


def _conformal_quantile(nc_scores, alpha):
    n_cal = len(nc_scores)
    return np.quantile(nc_scores, np.ceil((1-alpha)*(n_cal+1))/n_cal)


def _empirical_coverage(nc_scores, quantile):
    print(nc_scores)
    n_cov = len(nc_scores)  
    return np.sum(nc_scores < quantile)/n_cov


def _evaluate_model(train_series, test_series, model, output_dir, conformal_alpha, whole_series, calibration_series, coverage_series,  conformal_prediction_horizon):
    # Normalize the dataset
    scaler = Scaler()
    train_scaled = scaler.fit_transform(train_series)
    test_scaled = scaler.transform(test_series)
    whole_series_scaled = scaler.transform(series=whole_series)

    # Fit
    print('Fitting model...')
    training_start_time = time.time()
    if isinstance(model, (LinearRegressionModel, AutoARIMA, RandomForest, LightGBMModel, Prophet)):
        model.fit(series=train_scaled)
    else:
        model.fit(series=train_scaled, val_series=test_scaled)
    training_end_time = time.time()
    print('Model fitted!')

    # Predict validation
    print('Predicting validation...')
    n_steps = len(test_scaled)
    predicting_start_time = time.time()
    test_pred_scaled = model.predict(n=n_steps)
    predicting_end_time = time.time()
    test_pred_series = scaler.inverse_transform(test_pred_scaled)
    print('Validation predicted!')
    
    print(f'Predicting conformal calibration over horizon of {conformal_prediction_horizon} days...')
    # Conformal calibration
    predictions_calibration = []  # Will contain the series of predictions for the calibration for periods of conformal_prediction_horizon
    start_past_time = whole_series.start_time()
    end_past_time = calibration_series.start_time() - pd.Timedelta(days=1)
    while end_past_time <= calibration_series.end_time()-pd.Timedelta(days=conformal_prediction_horizon):
        if isinstance(model, (AutoARIMA, Prophet)):
            model.fit(series=whole_series_scaled[start_past_time:end_past_time])
            pred = model.predict(n=conformal_prediction_horizon)
        else:
            pred = model.predict(n=conformal_prediction_horizon, series=whole_series_scaled[start_past_time:end_past_time])
        predictions_calibration.append(scaler.inverse_transform(pred))
        end_past_time = end_past_time + pd.Timedelta(days=conformal_prediction_horizon)
        start_past_time = start_past_time + pd.Timedelta(days=conformal_prediction_horizon)
        
    # print(predictions_calibration[0].pd_dataframe())
    # print(predictions_calibration[1].pd_dataframe())
    # print(whole_series[calibration_series.start_time():calibration_series.start_time()+pd.Timedelta(days=10)].pd_dataframe())
        
    print(f'Predicting conformal test over horizon of {conformal_prediction_horizon} days...')
    # Empirical coverage predictions
    predictions_coverage = []  # Will contain the series of predictions for the coverage for periods of conformal_prediction_horizon
    start_past_time = whole_series.start_time() + pd.Timedelta(days=len(calibration_series))
    end_past_time = coverage_series.start_time() - pd.Timedelta(days=1)
    while end_past_time <= coverage_series.end_time()-pd.Timedelta(days=conformal_prediction_horizon):
        if isinstance(model, (AutoARIMA, Prophet)):
            model.fit(series=whole_series_scaled[start_past_time:end_past_time])
            pred = model.predict(n=conformal_prediction_horizon)
        else:
            pred = model.predict(n=conformal_prediction_horizon, series=whole_series_scaled[start_past_time:end_past_time])
        predictions_coverage.append(scaler.inverse_transform(pred))
        end_past_time = end_past_time + pd.Timedelta(days=conformal_prediction_horizon)
        start_past_time = start_past_time + pd.Timedelta(days=conformal_prediction_horizon)
        
    # print(predictions_coverage[0].pd_dataframe())
    # print(predictions_coverage[1].pd_dataframe())
    # print(whole_series[coverage_series.start_time():coverage_series.start_time()+pd.Timedelta(days=10)].pd_dataframe())

    # Rename variables
    actual_series = test_series
    pred_series = test_pred_series
    
    # ---- Save predictions
    print('Computing metrics...')
    actual_series_df = actual_series.pd_dataframe().reset_index()[['ticket_date', 'net_weight_kg']]
    actual_series_df = actual_series_df[['ticket_date', 'net_weight_kg']].rename(columns={'net_weight_kg': 'actual_net_weight_kg'})
    pred_series_df = pred_series.pd_dataframe().reset_index()
    pred_series_df = pred_series_df[['ticket_date', 'net_weight_kg']].rename(columns={'net_weight_kg': 'predicted_net_weight_kg'})

    output_df = pd.merge(actual_series_df, pred_series_df, on='ticket_date')

    def get_history_over_time(fun):
        hist = []
        for i in range(len(actual_series)):
            hist_val = fun(actual_series[i: i + 1], pred_series[i: i + 1])
            hist.append(hist_val)
        return hist

    output_df['rmse'] = get_history_over_time(rmse)
    output_df['mae'] = get_history_over_time(mae)


    # Compute non-conformity scores for conformal intervals
    print("Computing non-conformity scores...")
    actual_calibration_series = calibration_series
    actual_coverage_series = coverage_series
    
    calibration_scores = _compute_non_conformity_scores(mae, predictions_calibration, actual_calibration_series)
    coverage_scores = _compute_non_conformity_scores(mae, predictions_coverage, actual_coverage_series)

    # ---- Save summary

    mape_val = None
    try:
        mape_val = mape(actual_series, pred_series)
    except:
        pass
    
    conformal_quantile = _conformal_quantile(calibration_scores, conformal_alpha)

    summary = {
        'rmse': rmse(actual_series, pred_series),
        'mae': mae(actual_series, pred_series),
        'mape': mape_val,
        'conformal_quantile': conformal_quantile,
        'empirical_coverage': _empirical_coverage(coverage_scores, conformal_quantile),
        'r2_score': r2_score(actual_series, pred_series),
        'training_time': training_end_time - training_start_time,
        'predicting_time': predicting_end_time - predicting_start_time
    }
    with open(os.path.join(output_dir, 'summary.json'), 'w') as f:
        json.dump(summary, f)

    output_df.to_csv(os.path.join(output_dir, 'output.csv'), index=False)

    print(summary)
    
    print("Done.")


def run(params, generate_model_name, generate_model, conformal_alpha=0.05, conformal_prediction_horizon=5):
    dataset_name = params['dataset_name']
    test_calibration_split_before = params['test_calibration_split_before']
    empirical_coverage_split_before = params['empirical_coverage_split_before']
    only_weekdays = params['only_weekdays']
    is_differenced = params['is_differenced']

    # Config
    daily_waste_data_file_path = os.path.join(PROCESSED_DATA_PASS, dataset_name, 'imputed_data.csv')
    result_output_dir_path = os.path.join(RESULTS_PATH, dataset_name)

    # Create result dir
    if not os.path.exists(result_output_dir_path):
        os.makedirs(result_output_dir_path)

    # Preprocess
    df = pd.read_csv(daily_waste_data_file_path)
    df['ticket_date'] = df['ticket_date'].astype('datetime64[ns]')

    # Merge weekends to Monday
    if only_weekdays:
        df['year'] = df['ticket_date'].dt.year
        df['week'] = df['ticket_date'].dt.strftime('%U').astype(int)
        df['day_of_week'] = df['ticket_date'].dt.dayofweek

        # Tue, Wed, Thu, Fri
        df_part_1 = df.loc[df['day_of_week'].isin([1, 2, 3, 4])].copy().reset_index(drop=True)

        # Mon
        df_part_2 = df.loc[df['day_of_week'] == 0].copy().reset_index(drop=True)

        # Sat, Sun
        df_part_3 = df.loc[df['day_of_week'].isin([5, 6])].copy().reset_index(drop=True)
        df_part_3_copy = df_part_3.copy()
        df_part_3['week'] = df_part_3['week'] + 1
        df_part_3 = df_part_3.groupby(['year', 'week']).agg('sum').reset_index()
        df_part_3 = df_part_3[['year', 'week', 'net_weight_kg']].rename({'net_weight_kg': 'weekend_net_weight_kg'},
                                                                        axis='columns')

        df_part_23 = pd.merge(df_part_2, df_part_3, how='left', left_on=['year', 'week'], right_on=['year', 'week'])

        # Add weekend weight to Monday
        df_part_23['net_weight_kg'] = df_part_23['net_weight_kg'] + df_part_23['weekend_net_weight_kg']

        # Merge all data back together
        del df_part_3_copy['net_weight_kg']
        df = pd.concat([df_part_1, df_part_23, df_part_3_copy])

        df = df[['ticket_date', 'net_weight_kg']].sort_values(by=['ticket_date'], ascending=True).reset_index(drop=True)

        # Fill missing values - linear
        df[['net_weight_kg']] = df[['net_weight_kg']].fillna(value=0)

    # Apply differencing
    if is_differenced:
        df['net_weight_kg_diff'] = df['net_weight_kg'].diff(periods=1).fillna(df['net_weight_kg'])
        df['net_weight_kg'] = df['net_weight_kg_diff']
        del df['net_weight_kg_diff']

    # Model
    model_name = generate_model_name(params)
    model = generate_model(params)

    # Output directory path
    output_dir = '{}/{}'.format(result_output_dir_path, model_name)

    # Check if output dir and params exists
    if os.path.exists(output_dir) and os.path.exists('{}/summary.json'.format(output_dir)):
        print('Skip: {} - {}'.format(dataset_name, model_name))
        return
    else:
        print('Run: {} - {}'.format(dataset_name, model_name))

    # Create output directory
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Convert to a Darts Timeseries
    series = TimeSeries.from_dataframe(df, time_col='ticket_date', value_cols='net_weight_kg')

    # Split into train and test series
    train_series, test_series = series.split_before(pd.Timestamp(test_calibration_split_before))
    calibration_series, empirical_coverage_series = test_series.split_before(pd.Timestamp(empirical_coverage_split_before))

    # Evaluate test
    _evaluate_model(train_series, test_series, model, output_dir, conformal_alpha=conformal_alpha, whole_series=series, calibration_series=calibration_series, coverage_series=empirical_coverage_series, conformal_prediction_horizon=conformal_prediction_horizon)

    # Save params
    with open('{}/params.json'.format(output_dir), 'w') as f:
        json.dump(params, f)

    # Clear figures
    plt.close('all')
