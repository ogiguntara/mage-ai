from flask import Flask, render_template, request
from flask_cors import CORS
from mage_ai.data_cleaner.data_cleaner import analyze, clean as clean_data
from mage_ai.data_cleaner.pipelines.base import BasePipeline
from mage_ai.server.constants import SERVER_PORT
from mage_ai.server.data.models import FeatureSet, Pipeline
from numpyencoder import NumpyEncoder
import logging
import json
import simplejson
import sys
import threading

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__,
            static_url_path='',
            static_folder='frontend_dist',
            template_folder='frontend_dist')

CORS(app, resources={r'/*': {'origins': '*'}})

thread = None


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/process', methods=['POST'])
def process():
    """
    request: {
        id: string (feature set id)
        clean: boolean
    }

    response: {
        id,
        metadata,
        sample_data,
        statistics,
        insights,
        suggestions
    }
    """
    request_data = request.json
    if not request_data:
        request_data = request.form

    id = request_data['id']
    if not id:
        return

    feature_set = FeatureSet(id=id)
    df = feature_set.data
    metadata = feature_set.metadata

    if request_data.get('clean', True):
        result = clean_data(df)
    else:
        result = analyze(df)

    feature_set.write_files(result)
    
    column_types = result['column_types']
    metadata['column_types'] = column_types

    feature_set.metadata = metadata

    response = app.response_class(
        response=simplejson.dumps(feature_set.to_dict(), ignore_nan=True),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route('/feature_sets')
def feature_sets():
    """
    response: [
        {
            id,
            metadata,
        }
    ]
    """
    feature_sets = list(map(lambda fs: fs.to_dict(detailed=False), FeatureSet.objects()))
    valid_feture_sets = [f for f in feature_sets
                         if set(['column_types', 'statistics']).issubset(f['metadata'].keys())]
    response = app.response_class(
        response=simplejson.dumps(valid_feture_sets, ignore_nan=True),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route('/feature_sets/<id>')
def feature_set(id):
    """
    response: [
        {
            id,
            metadata,
        }
    ]
    """
    feature_set = FeatureSet(id=id)
    query_column = request.args.get('column')
    response = app.response_class(
        response=simplejson.dumps(feature_set.to_dict(column=query_column), ignore_nan=True),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route('/feature_sets/<id>', methods=['PUT'])
def update_feature_set(id):
    """
    request: {
        metadata,
        statistics,
        insights,
        suggestions,
    }

    response: {
        id,
        metadata,
        pipeline,
        sample_data,
        statistics,
        insights,
        suggestions,
    }
    """
    request_data = request.json
    feature_set = FeatureSet(id=id)
    feature_set.write_files(request_data)
    response = app.response_class(
        response=simplejson.dumps(feature_set.to_dict(), ignore_nan=True),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route('/pipelines')
def pipelines():
    """
    response: [
        {
            id,
            pipeline_actions,
        }
    ]
    """
    pipelines = list(map(lambda p: p.to_dict(detailed=False), Pipeline.objects()))
    response = app.response_class(
        response=json.dumps(pipelines, cls=NumpyEncoder),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route('/pipelines/<id>')
def pipeline(id):
    """
    response: {
        id,
        actions,
    }
    """
    pipeline = Pipeline(id=id)
    response = app.response_class(
        response=json.dumps(pipeline.to_dict(), cls=NumpyEncoder),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route('/pipelines/<id>', methods=['PUT'])
def update_pipeline(id):
    """
    request: {
        actions,
    }

    response: {
        id,
        actions,
    }
    """
    request_data = request.json
    pipeline = Pipeline(id=id)
    actions = request_data.get('actions', [])
    clean_pipeline = BasePipeline(actions=actions)
    pipeline.pipeline = clean_pipeline
    # # 1. Transform the data
    # # 2. Recalculate stats and suggestions
    feature_set_id = pipeline.metadata.get('feature_set_id')
    if feature_set_id is not None:
        feature_set = FeatureSet(id=feature_set_id)
        df_transformed = clean_pipeline.transform(feature_set.data_orig, auto=False)
        result = clean_data(df_transformed, transform=False)
        feature_set.write_files(result)

    response = app.response_class(
        response=json.dumps(pipeline.to_dict(), cls=NumpyEncoder),
        status=200,
        mimetype='application/json'
    )
    return response

# @app.route("/feature_sets/<id>/columns/<column_name>")
# def feature_set_column(id, column_name):
#     feature_set = FeatureSet(id=id)
#     return feature_set.column(column_name)


def clean_df(df, name, pipeline_uuid=None):
    feature_set = FeatureSet(df=df, name=name)

    result = clean_data(df)

    feature_set.write_files(result)
    return (feature_set, result['df'])


def connect_df(df, name):
    feature_set = FeatureSet(df=df, name=name)

    result = clean_data(df, transform=False)

    feature_set.write_files(result, write_orig_data=True)
    return (feature_set, df)


class ThreadWithTrace(threading.Thread):
    def __init__(self, *args, **keywords):
        threading.Thread.__init__(self, *args, **keywords)
        self.killed = False

    def start(self):
        self.__run_backup = self.run
        self.run = self.__run
        threading.Thread.start(self)

    def __run(self):
        sys.settrace(self.globaltrace)
        self.__run_backup()
        self.run = self.__run_backup

    def globaltrace(self, frame, event, arg):
        if event == 'call':
            return self.localtrace
        else:
            return None

    def localtrace(self, frame, event, arg):
        if self.killed:
            if event == 'line':
                raise SystemExit()
        return self.localtrace

    def kill(self):
        self.killed = True


def launch() -> None:
    global thread
    app_kwargs = {'port': SERVER_PORT, 'host': 'localhost', 'debug': False}
    thread = ThreadWithTrace(target=app.run, kwargs=app_kwargs, daemon=True)
    thread.start()
    return thread


def kill():
    if thread is not None:
        thread.kill()
        thread.join()
        if not thread.isAlive():
            print('Flask server is terminated')
