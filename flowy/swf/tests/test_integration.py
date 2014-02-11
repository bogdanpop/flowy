import os
import unittest
import json
from boto.swf.layer1 import Layer1
import workflows
from flowy.swf.boilerplate import start_workflow_worker
from itertools import cycle


class MockLayer1(Layer1):

    def __init__(self, responses, requests):
        self.responses = cycle(responses)
        self.requests = cycle(requests)

    def json_request(self, action, data, object_hook=None):
        return next(self.responses)


def make(file_name):
    f = open(os.path.join(here, 'logs', file_name))
    responses = []
    requests = []
    for line in f:
        line = line.split('\t')
        if line[0] == '<<<':
            res = json.loads(line[1])
            if res is not None:
                responses.append(res)
        else:
            requests.append((line[1], json.loads(line[2])))

    mock_layer1 = MockLayer1(responses, requests)

    def test(self):
        start_workflow_worker('TestDomain', 'test_list',
                              layer1=mock_layer1,
                              reg_remote=False,
                              package=workflows,
                              loop=10)
    f.close()
    return test


class ExamplesTest(unittest.TestCase):
    pass


here = os.path.dirname(__file__)

for file_name in os.listdir(os.path.join(here, 'logs'))[:8]:
    test_name = 'test_' + file_name.rsplit('.', 1)[0]
    print 'adding:', test_name
    setattr(ExamplesTest, test_name, make(file_name))
