"""四要素来源映射、严格布尔与确定性正文投影。"""
from copy import deepcopy
import pytest
from mmwf.domain.review.gate_projection import display_topics, system_text


@pytest.mark.parametrize('confidence,low', [(0.69, True), (0.7, False), (None, False)])
def test_sources_and_threshold(confidence, low):
    state = {'topics': [{'id': 't', 'title': '预算', 'discussion': '讨论', 'readings': ['材料'],
                         'refs': [{'segment_id': 'seg-1', 'start_ms': 999}], 'confirmed': 'true'}],
             'resolutions': [{'topic_id': 't', 'text': '决议'}],
             'action_items': [{'topic': '预算', 'text': '落实'}]}
    before = deepcopy(state)
    topic = display_topics(state, [{'segment_id': 'seg-1', 'start_ms': 1234, 'text': '原文',
                                   'confidence': confidence}], 'high', 0.7)['topics'][0]
    assert topic['basis'][0]['start_ms'] == 1234
    assert topic['raw_text'] == '原文'
    assert topic['confidence'] == confidence and topic['low_confidence'] is low
    assert topic['confirmed'] is False and topic['risk'] == 'high'
    assert topic['extracted'] == '预算\n\n讨论\n\n材料\n\n决议\n\n落实'
    assert system_text('G2', state) == topic['extracted']
    assert state == before


def test_missing_sources_and_confirmed():
    state = {'topics': [{'title': '事项', 'confirmed': True, 'risk': 'low',
                         'refs': [{'segment_id': 'missing', 'text': '不可冒充转写原文'}]}, {'title': '另项'}]}
    topics = display_topics(state, [], 'medium', 0.7)['topics']
    assert topics[0]['raw_text'] == '暂无出处，请核对'
    assert topics[0]['basis'] == [] and topics[0]['confidence'] is None
    assert topics[0]['confirmed'] is True and topics[0]['risk'] == 'low'
    assert topics[1]['confirmed'] is False


def test_fixed_order_and_unassociated_items():
    state = {'topics': [{'readings': ['乙'], 'discussion': '甲', 'title': '标题'}],
             'action_items': [{'text': '事项'}], 'resolutions': [{'text': '决议'}]}
    assert system_text('G2', state) == '标题\n\n甲\n\n乙\n\n决议\n\n事项'
    assert system_text('G3', {'minutes_markdown': '原样\r\n\r\n正文'}) == '原样\r\n\r\n正文'
