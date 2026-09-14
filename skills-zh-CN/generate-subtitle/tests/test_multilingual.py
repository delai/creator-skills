"""Behavioral regressions; no downloads, GPU, speech models or LLM calls required."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate_subtitle as gen
import text_utils as text
import whisper_asr
import asr_router
import multi_track
import llm
import translate_subtitle as translation


class UnicodeTests(unittest.TestCase):
    def test_words_and_graphemes(self):
        for sentence, expected in [
            ('cafe\u0301 de\u0301ja\u0300', ['cafe\u0301', 'de\u0301ja\u0300']),
            ('Привет мир', ['Привет', 'мир']),
            ('مَرْحَبًا بالعالم', ['مَرْحَبًا', 'بالعالم']),
            ('नमस्ते दुनिया', ['नमस्ते', 'दुनिया']),
            ('안녕하세요 여러분', ['안녕하세요', '여러분']),
        ]:
            with self.subTest(sentence=sentence):
                self.assertEqual([sentence[a:b] for a,b in text.tokenize_spans(sentence)], expected)
        for sentence in ['ภาษาไทยทดสอบ', 'これはテストです', '你好世界', '👩🏽‍💻👨‍👩‍👧‍👦 e\u0301']:
            boundaries = {0, *(b for _, b in text.grapheme_spans(sentence))}
            pieces = gen.split_indices(sentence, 2)
            self.assertTrue(all(a in boundaries and b in boundaries for a,b in pieces))
            self.assertEqual(''.join(sentence[a:b] for a,b in pieces), sentence)

    def test_width(self):
        self.assertEqual(text.display_width('é'), text.display_width('e\u0301'))
        self.assertEqual(text.display_width('👩🏽‍💻'), 1)
        self.assertEqual(text.display_width('中A'), 1.5)

    def test_timestamp_mapping_and_alignment(self):
        sentence = 'cafe\u0301 世界'
        words = [{'word':'cafe\u0301', 'start':1000, 'end':2000}, {'word':' 世界','start':3000,'end':4000}]
        times = text.char_time_map(sentence, None, 1000, 4000, words)
        self.assertEqual(len(times), len(sentence))
        self.assertEqual(times[3], times[4])
        self.assertEqual(times[0][0], 1000)
        self.assertEqual(times[-1][1], 4000)
        self.assertTrue(all(a <= b for a,b in times))
        self.assertTrue(all(times[i][1] <= times[i+1][0] for i in [0,1,2,4,5,6]))
        result = {'language':'fr','segments':[{'text':sentence, 'start':1,'end':4,
                  'words':[dict(w,start=w['start']/1000,end=w['end']/1000) for w in words]}]}
        item = whisper_asr.to_sentences(result)[0]
        shifted = multi_track.shift_sentence(dict(item), multi_track.Alignment(offset_ms=5000, speed=1.001))
        self.assertAlmostEqual(shifted['words'][0]['start'], 6001)
        self.assertEqual(item['words'][0]['start'], 1000)
        cues = gen.sentence_to_cues(shifted, 4, 0, False)
        self.assertTrue(all(shifted['start'] <= c['start'] < c['end'] <= shifted['end'] for c in cues))

    def test_language_specific_rules(self):
        for language, source in [('en','OK'), ('en','abcabc soooo!'), ('ja','二零二五年AIを使います。'),
                                 ('fr','cafe\u0301 déjà!'), ('ar','مَرْحَبًا؟'), ('auto','二零二五年')]:
            cue = {'text':source,'language':language,'start':0,'end':1000}
            self.assertFalse(gen.apply_dedupe([cue]))
            self.assertFalse(gen.apply_year_digits([cue]))
            self.assertEqual(gen.rule_polish(source, language), source)
            self.assertEqual(gen.finalize_text(source, False, language), source)
            kept, dropped, _ = gen.polish_cues([cue], False, '', [], 100, 4000, level='clean')
            self.assertEqual(dropped, 0)
            self.assertEqual(kept[0]['text'], source)
        self.assertEqual(gen.normalize_year_digits('二零二五年', 'zh'), '2025年')
        self.assertEqual(gen.dedupe_stutter('我觉得我觉得abcabc', 'zh'), '我觉得abcabc')
        self.assertEqual(gen.finalize_text('使用AI', False, 'zh'), '使用 AI')

    def test_no_latin_word_or_grapheme_loss(self):
        source = 'Café déjà vu. Привет мир! नमस्ते दुनिया। مَرْحَبًا بالعالم؟'
        spans = gen.split_indices(source, 8)
        self.assertEqual(''.join(source[a:b] for a,b in spans), source)
        self.assertFalse(text.sentence_boundary('3.14', 1))
        self.assertTrue(text.sentence_boundary('Hello. Next.', 5))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root/'a.wav'
        self.source.write_bytes(b'audio')
        self.args = types.SimpleNamespace(no_cache=False, language='auto', whisper_model='small',
                                         device='cpu', engine='auto', speaker_count=None)
        self.whisper = Mock()
        self.whisper.detect.return_value = {'language':'ja', 'chinese':False, 'samples':[]}
        self.whisper.transcribe.return_value = [{'text':'日本語。','start':0,'end':1000,'language':'ja'}]
        self.loader = Mock()
        self.resolver = Mock(return_value='funasr')
        self.router = asr_router.ASRRouter(self.args, self.root, gen.cache_path, self.resolver, self.loader)
        self.router.models['whisper'] = self.whisper

    def test_non_chinese_and_resume(self):
        self.assertEqual(self.router.transcribe(self.source, 'L', lambda:self.source)[0]['language'], 'ja')
        self.assertEqual(self.router.records[0]['engine'], 'whisper')
        self.whisper.detect.assert_called_once()
        self.whisper.transcribe.assert_called_once_with(self.source, 'auto')
        self.loader.assert_not_called()
        self.router.transcribe(self.source,'L', Mock(side_effect=AssertionError('cache must not extract audio')))
        self.whisper.detect.assert_called_once()
        self.whisper.transcribe.assert_called_once()
        self.args.whisper_model = 'medium'
        self.router.transcribe(self.source,'L',lambda:self.source)
        self.assertEqual(self.whisper.detect.call_count,2)
        self.assertEqual(self.whisper.transcribe.call_count,2)

    def test_chinese_retains_original_route(self):
        self.args.language='zh'
        self.loader.return_value = Mock(ENGINE='firered')
        self.loader.return_value.transcribe.return_value = [{'text':'中文','start':0,'end':1000}]
        self.resolver.return_value='firered'
        result = self.router.transcribe(self.source, '', lambda:self.source)
        self.resolver.assert_called_with('auto',False)
        self.whisper.detect.assert_not_called()
        self.assertEqual(result[0]['language'],'zh')
        self.assertEqual(result[0]['engine'],'firered')

    def test_voiceprint_scope_and_physical_tracks(self):
        with self.assertRaisesRegex(ValueError,'中文'):
            self.router.transcribe(self.source, '', lambda:self.source, diarize=True)
        self.assertTrue(self.router.transcribe(self.source, 'physical-track', lambda:self.source, diarize=False))

    def test_cache_identity(self):
        names = [gen.cache_path(self.root,self.source,False,engine=e,language=l,model=m)
                 for e,l,m in [('whisper','en','small'),('whisper','ja','small'),
                               ('whisper','en','medium'),('funasr','en','small')]]
        self.assertEqual(len(set(names)),4)

    def test_model_support_and_task(self):
        tokenizer = types.ModuleType('whisper.tokenizer')
        tokenizer.get_tokenizer=Mock(return_value=types.SimpleNamespace(all_language_codes=('en','ja')))
        api = types.ModuleType('whisper')
        model = Mock(is_multilingual=True,num_languages=2)
        model.transcribe.return_value={'segments':[], 'language':'ja'}
        api.load_model=Mock(return_value=model)
        with patch.dict(sys.modules, {'whisper':api,'whisper.tokenizer':tokenizer}):
            adapter=whisper_asr.WhisperASR('small','mps')
            api.load_model.assert_called_with('small',device='cpu')
            with self.assertRaises(ValueError):
                adapter.validate_language('yue')
            adapter.transcribe(self.source,'ja')
            self.assertEqual(model.transcribe.call_args.kwargs['task'],'transcribe')
            self.assertEqual(model.transcribe.call_args.kwargs['language'],'ja')
            self.assertFalse(model.transcribe.call_args.kwargs['fp16'])
            adapter.transcribe(self.source,'auto')
            self.assertIsNone(model.transcribe.call_args.kwargs['language'])
            with self.assertRaises(ValueError):
                whisper_asr.supported_languages(types.SimpleNamespace(is_multilingual=False))

    def test_device_priority(self):
        for cuda,mps,expected in [(True,True,'cuda'),(False,True,'mps'),(False,False,'cpu')]:
            torch=types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda:cuda),
                       backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda:mps)))
            with patch.dict(sys.modules, {'torch':torch}):
                self.assertEqual(gen.resolve_device('auto'),expected)


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        self.source=self.root/'talk.srt'
        self.raw='7\r\n00:00:01,000 --> 00:00:03,000\r\nspk1: 你好 cafe\u0301\r\nspk2: 日本語\r\n\r\n11\r\n00:00:02,000 --> 00:00:04,000\r\n<v Alice>مرحبا</v>\r\n'
        self.source.write_bytes(self.raw.encode())
        self.addCleanup(llm._pending.clear)
        llm._pending.clear()
        self.env=patch.dict(os.environ, {'SUBTITLE_LLM':'1','SUBTITLE_HANDOFF_DIR':str(self.root/'tasks')})
        self.env.start(); self.addCleanup(self.env.stop)

    def fill(self, bad=False, uncertain=False):
        for task in (self.root/'tasks').iterdir():
            if not task.is_dir(): continue
            data=json.loads((task/'in.json').read_text())['cues']
            out=[{'id':x['id'],'text':('spk1: Hello cafe\u0301\nspk2: Japanese' if x['id']==0 else '<v Alice>Hello</v>')} for x in data]
            if bad: out[0]['text']=out[0]['text'].replace('spk2:', 'spk9:')
            if uncertain: out[0]['uncertain']='原文含糊'
            (task/'out.json').write_text(json.dumps(out,ensure_ascii=False))

    def test_resume_preserves_everything_except_text(self):
        self.assertEqual(translation.translate_file(self.source,'en'),10)
        self.assertFalse((self.root/'talk.en.srt').exists())
        self.fill()
        self.assertEqual(translation.translate_file(self.source,'en'),0)
        self.assertEqual(self.source.read_bytes(),self.raw.encode())
        output=(self.root/'talk.en.srt').read_bytes().decode()
        _,before=translation.parse_document(self.raw)
        _,after=translation.parse_document(output)
        self.assertEqual([x['header'] for x in before],[x['header'] for x in after])
        self.assertEqual([x['markers'] for x in before],[x['markers'] for x in after])
        self.assertIn('Hello',output)
        self.assertEqual(translation.translate_file(self.source,'en'),0)

    def test_invalid_answer_reopens_task(self):
        translation.translate_file(self.source,'en'); self.fill(bad=True)
        self.assertEqual(translation.translate_file(self.source,'en'),10)
        self.assertFalse((self.root/'talk.en.srt').exists())
        self.assertTrue(list((self.root/'tasks').glob('*/out.invalid.json')))
        self.fill()
        self.assertEqual(translation.translate_file(self.source,'en'),0)

    def test_uncertain_source_and_translation(self):
        marker=self.root/'talk.uncertain.srt'; marker.write_text('听不清')
        self.assertEqual(translation.translate_file(self.source,'en'),2)
        self.assertFalse((self.root/'tasks').exists())
        marker.unlink()
        translation.translate_file(self.source,'en');self.fill(uncertain=True)
        self.assertEqual(translation.translate_file(self.source,'en'),2)
        self.assertFalse((self.root/'talk.en.srt').exists())
        self.fill()
        self.assertEqual(translation.translate_file(self.source,'en'),0)
        self.assertFalse((self.root/'talk.translation-en.uncertain.json').exists())

    def test_target_and_source_identity(self):
        with self.assertRaises(ValueError): translation.translate_file(self.source,'auto')
        with self.assertRaises(ValueError): translation.translate_file(self.source,'en',self.source)
        translation.translate_file(self.source,'en'); self.fill()
        translation.translate_file(self.source,'ja')
        self.assertEqual(len(list((self.root/'tasks').glob('*/TASK.md'))),2)
        self.source.write_text(self.raw.replace('你好','您好'))
        translation.translate_file(self.source,'en')
        self.assertEqual(len(list((self.root/'tasks').glob('*/TASK.md'))),3)

    def test_vtt_settings_and_no_translation_growth_limit(self):
        self.source=self.root/'talk.vtt'
        raw='WEBVTT\n\ncue-α\n00:00:01.000 --> 00:00:03.000 align:start position:10%\n<v Alice>猫</v>\n'
        self.source.write_text(raw)
        translation.translate_file(self.source,'en')
        task=next((self.root/'tasks').glob('translate-*'))
        (task/'out.json').write_text(json.dumps([{'id':0,'text':'<v Alice>A much longer translated description of a cat.</v>'}]))
        self.assertEqual(translation.translate_file(self.source,'en'),0)
        output=(self.root/'talk.en.vtt').read_text()
        self.assertIn('cue-α\n00:00:01.000 --> 00:00:03.000 align:start position:10%',output)
        self.assertEqual(self.source.read_text(),raw)

    def test_cli_existing_subtitle_needs_no_asr(self):
        run=subprocess.run([sys.executable,str(Path(gen.__file__)),str(self.source),'--translate-to','en','--dry-run'], capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        self.assertFalse((self.root/'tasks').exists())


if __name__=='__main__':
    unittest.main()
