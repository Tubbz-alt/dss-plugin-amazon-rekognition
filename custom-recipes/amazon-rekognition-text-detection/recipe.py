# -*- coding: utf-8 -*-
import json
import logging
from typing import Dict, AnyStr
from ratelimit import limits, RateLimitException
from retry import retry

import dataiku
from dataiku.customrecipe import get_recipe_config, get_input_names_for_role, get_output_names_for_role
import pandas as pd

from amazon_rekognition_api_client import API_EXCEPTIONS, get_client, supported_image_format
from plugin_io_utils import IMAGE_PATH_COLUMN, ErrorHandlingEnum, generate_path_list, set_column_description
from api_parallelizer import api_parallelizer
from amazon_rekognition_api_formatting import GenericAPIFormatter


# ==============================================================================
# SETUP
# ==============================================================================

input_folder_names = get_input_names_for_role("input_folder")
input_folder = dataiku.Folder(input_folder_names[0])
input_folder_is_s3 = input_folder.get_info().get("type", "") == "S3"
if input_folder_is_s3:
    logging.info("Input folder is on Amazon S3")
    input_folder_access_info = input_folder.get_info().get("accessInfo", {})
    input_folder_bucket = input_folder_access_info.get("bucket")
    input_folder_root_path = str(input_folder_access_info.get("root", ""))[1:]

output_dataset_names = get_output_names_for_role("output_dataset")  # mandatory output
output_dataset = dataiku.Dataset(output_dataset_names[0])

output_folder_names = get_output_names_for_role("output_folder")  # optional output
output_folder = None
if len(output_folder_names) != 0:
    output_folder = dataiku.Folder(output_folder_names[0])

recipe_config = get_recipe_config()
api_configuration_preset = recipe_config.get("api_configuration_preset")
api_quota_rate_limit = api_configuration_preset.get("api_quota_rate_limit")
api_quota_period = api_configuration_preset.get("api_quota_period")
parallel_workers = api_configuration_preset.get("parallel_workers")
num_objects = int(recipe_config.get("num_objects", 1))
minimum_score = int(recipe_config.get("minimum_score", 0)) * 100
if minimum_score < 0 or minimum_score > 100:
    raise ValueError("Minimum confidence score must be between 0 and 1")
error_handling = ErrorHandlingEnum[recipe_config.get("error_handling")]

client = get_client(api_configuration_preset)
column_prefix = "text_api"


# ==============================================================================
# RUN
# ==============================================================================

image_path_list = [p for p in generate_path_list(input_folder) if supported_image_format(p)]
input_df = pd.DataFrame(image_path_list, columns=[IMAGE_PATH_COLUMN])


@retry((RateLimitException, OSError), delay=api_quota_period, tries=5)
@limits(calls=api_quota_rate_limit, period=api_quota_period)
def call_api_text_detection(row: Dict, num_objects: int, minimum_score: int) -> AnyStr:
    image_path = row.get(IMAGE_PATH_COLUMN)
    if input_folder_is_s3:
        image_request = {"S3Object": {"Bucket": input_folder_bucket, "Name": input_folder_root_path + image_path}}
    else:
        with input_folder.get_download_stream(image_path) as stream:
            image_request = {"Bytes": stream.read()}
    response = client.detect_text(Image=image_request)
    return json.dumps(response)


df = api_parallelizer(
    input_df=input_df,
    api_call_function=call_api_text_detection,
    api_exceptions=API_EXCEPTIONS,
    column_prefix=column_prefix,
    parallel_workers=parallel_workers,
    error_handling=error_handling,
    num_objects=num_objects,
    minimum_score=minimum_score,
)

api_formatter = GenericAPIFormatter(
    input_df=input_df,
    input_folder=input_folder,
    column_prefix=column_prefix,
    error_handling=error_handling,
    parallel_workers=parallel_workers,
)
output_df = api_formatter.format_df(df)

output_dataset.write_with_schema(output_df)
set_column_description(output_dataset=output_dataset, column_description_dict=api_formatter.column_description_dict)

if output_folder is not None:
    api_formatter.save_bounding_boxes_all_images(output_folder)
