import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DETECTOR = os.path.dirname(HERE)
# zip と同じく、detector の直下と common の親(lambdas/)を import パスに入れる
sys.path[:0] = [DETECTOR, os.path.dirname(DETECTOR)]

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.pop("AWS_PROFILE", None)

FIXTURES = os.path.join(HERE, "fixtures")
MODELS = os.path.join(DETECTOR, "models")
