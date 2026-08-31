"""Tests for the MiniMax M3 reasoning parser plugin."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _ensure_fake_vllm_modules():
    """Load the plugin without requiring the vLLM runtime."""
    vllm = sys.modules.get('vllm')
    if vllm is None:
        vllm = types.ModuleType('vllm')
        sys.modules['vllm'] = vllm

    if not hasattr(vllm, 'entrypoints'):
        entrypoints = types.ModuleType('vllm.entrypoints')
        openai = types.ModuleType('vllm.entrypoints.openai')
        engine = types.ModuleType('vllm.entrypoints.openai.engine')
        protocol = types.ModuleType(
            'vllm.entrypoints.openai.engine.protocol')

        class DeltaMessage:
            def __init__(self, reasoning=None, content=None):
                self.reasoning = reasoning
                self.content = content

        protocol.DeltaMessage = DeltaMessage
        vllm.entrypoints = entrypoints
        entrypoints.openai = openai
        openai.engine = engine
        engine.protocol = protocol
        sys.modules.update({
            'vllm.entrypoints': entrypoints,
            'vllm.entrypoints.openai': openai,
            'vllm.entrypoints.openai.engine': engine,
            'vllm.entrypoints.openai.engine.protocol': protocol,
        })

    if not hasattr(vllm, 'reasoning'):
        reasoning = types.ModuleType('vllm.reasoning')
        basic = types.ModuleType('vllm.reasoning.basic_parsers')

        class BaseThinkingReasoningParser:
            def __init__(self, tokenizer=None, *args, **kwargs):
                self.tokenizer = tokenizer
                self._initial_in_reasoning = False
                self.start_token_id = None
                self.end_token_id = None

            def count_reasoning_tokens(self, token_ids):
                return len(token_ids)

        class ReasoningParserManager:
            @staticmethod
            def register_module(_name):
                return lambda cls: cls

        basic.BaseThinkingReasoningParser = BaseThinkingReasoningParser
        reasoning.ReasoningParserManager = ReasoningParserManager
        reasoning.basic_parsers = basic
        vllm.reasoning = reasoning
        sys.modules.update({
            'vllm.reasoning': reasoning,
            'vllm.reasoning.basic_parsers': basic,
        })


def _load_parser_module():
    _ensure_fake_vllm_modules()
    path = Path(__file__).resolve().parents[1] / \
        'minimax_m3_reasoning_parser.py'
    spec = importlib.util.spec_from_file_location(
        'minimax_m3_reasoning_parser', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_parser_module()


def make_parser(initial=False):
    parser = MODULE.MiniMaxM3ReasoningParser(
        None, chat_template_kwargs=(
            {'thinking_mode': 'enabled'} if initial else {}))
    parser.start_token_id = 1
    parser.end_token_id = 2
    return parser


def stream(parser, delta_text, delta_ids, previous_ids=()):
    message = parser.extract_reasoning_streaming(
        '', '', delta_text, previous_ids, [], delta_ids)
    return None if message is None else (message.reasoning, message.content)


class MiniMaxParserTests(unittest.TestCase):
    def test_extract_reasoning_without_initial_block(self):
        parser = make_parser()
        self.assertEqual(parser.extract_reasoning('</mm:think>body', None),
                         (None, 'body'))
        self.assertEqual(parser.extract_reasoning('</mm:think>later', None),
                         (None, 'later'))
        self.assertEqual(parser.extract_reasoning('plain', None), (None, 'plain'))
        self.assertEqual(parser.extract_reasoning('<mm:think>idea</mm:think>', None),
                         ('idea', None))
        self.assertEqual(
            parser.extract_reasoning('before<mm:think>idea</mm:think>after', None),
            ('idea', 'beforeafter'))
        self.assertEqual(
            parser.extract_reasoning('before<mm:think>unfinished', None),
            ('unfinished', 'before'))

    def test_extract_reasoning_with_initial_block(self):
        parser = make_parser(initial=True)
        self.assertEqual(parser.extract_reasoning('whole reasoning', None),
                         ('whole reasoning', None))
        self.assertEqual(
            parser.extract_reasoning('reasoning</mm:think>content', None),
            ('reasoning', 'content'))
        self.assertEqual(
            parser.extract_reasoning('<mm:think>reasoning</mm:think>content', None),
            ('reasoning', 'content'))

    def test_reasoning_end_and_content_ids(self):
        parser = make_parser()
        self.assertTrue(parser.is_reasoning_end_streaming([], [2]))
        self.assertTrue(parser.is_reasoning_end_streaming([2], []))
        self.assertFalse(parser.is_reasoning_end_streaming([1], []))
        self.assertFalse(parser.is_reasoning_end_streaming([], []))
        self.assertTrue(parser.is_reasoning_end_streaming([1, 2], [2]))

        self.assertEqual(parser.extract_content_ids([1, 2, 3, 2, 4]), [4])
        self.assertEqual(parser.extract_content_ids([1, 3]), [])
        self.assertEqual(parser.extract_content_ids([3, 4]), [3, 4])
        self.assertEqual(parser.extract_content_ids([1]), [])

        initial = make_parser(initial=True)
        self.assertEqual(initial.extract_content_ids([3, 4]), [])

    def test_streaming_leading_closer(self):
        self.assertIsNone(stream(make_parser(), '', [3]))
        self.assertIsNone(stream(make_parser(), '</mm:think>', [2]))
        self.assertEqual(stream(make_parser(), '</mm:think>body', [2, 3]),
                         (None, 'body'))
        parser = make_parser()
        self.assertEqual(stream(parser, '</mm:think>body', [3]),
                         (None, 'body'))
        self.assertFalse(parser._at_response_start)

    def test_streaming_initial_reasoning(self):
        parser = make_parser(initial=True)
        self.assertEqual(stream(parser, 'partial', [3]), ('partial', None))
        self.assertEqual(stream(parser, 'partial</mm:think>body', [2, 3]),
                         ('partial', 'body'))

    def test_streaming_after_start_marker(self):
        parser = make_parser()
        self.assertEqual(stream(parser, 'body', [3], previous_ids=[2]),
                         (None, 'body'))
        self.assertEqual(
            stream(parser, 'reasoning</mm:think>body', [2, 3],
                   previous_ids=[1]),
            ('reasoning', 'body'))
        self.assertEqual(
            stream(parser, '<mm:think>reasoning</mm:think>body', [1, 2, 3]),
            ('reasoning', 'body'))
        self.assertEqual(stream(parser, '<mm:think>reasoning', [1, 3]),
                         ('reasoning', None))
        self.assertIsNone(stream(parser, '<mm:think>', [1]))
        self.assertEqual(stream(parser, 'reasoning', [3], previous_ids=[1]),
                         ('reasoning', None))

    def test_count_reasoning_tokens(self):
        parser = make_parser()
        self.assertEqual(parser.count_reasoning_tokens([4, 5]), 2)
        initial = make_parser(initial=True)
        tokens = [1, 4, 1, 5, 2, 6, 2, 7]
        self.assertEqual(initial.count_reasoning_tokens(tokens), 4)

    def test_leading_closer_helpers(self):
        parser = make_parser()
        self.assertEqual(parser._strip_leading_closer('body', [3]),
                         ('body', [3]))
        self.assertEqual(parser._strip_leading_closer('</mm:think>body', [2]),
                         ('body', []))
        self.assertIsNone(parser._strip_leading_closer('</mm:think>', [2])[0])


if __name__ == '__main__':
    unittest.main()