"""immune - an artificial-immune-system style IDS for serverless APIs.

Only the inference side (features, model, store, guard) has to ship inside the Lambda package and
it needs nothing but the standard library (plus boto3, which the Lambda runtime provides).
Training (`immune.train`) uses numpy and never goes into the package.
"""

__version__ = "0.1.0"
