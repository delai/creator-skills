import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate_subtitle as gen
import llm
import translate_subtitle as translation


class PipelineTests(unittest.TestCase):
    def test_existing_vtt_short_timestamps(self):
        raw='WEBVTT\n\na-7\n00:01.000 --> 00:03.000 align:start\nspk1: 你好\n'
        _, cues=translation.parse_document(raw)
        self.assertIn('00:01.000 --> 00:03.000',cues[0]['header'])
        self.assertEqual(translation.stamp_ms('00:01.000'),1000)

    def test_vtt_uncertain_format_and_translation_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'talk.vtt'
            source.write_text('WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\ncafé\n')
            uncertain=gen.write_uncertain(source,[{'time':'00:00:01,000','text':'café','candidates':[]}])
            raw=uncertain.read_text()
            self.assertTrue(raw.startswith('WEBVTT\n'))
            self.assertIn('00:00:01.000 --> 00:00:04.000',raw)
            self.assertEqual(translation.translate_file(source,'en'),2)

    @unittest.skipIf(gen.transcript_lib is None, 'optional podcast transcript exporter unavailable')
    def test_optional_transcript_preserves_grapheme_timestamps(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root=Path(directory)
            output=root/'talk.srt'
            raw='cafe\u0301'
            gen.TRANSCRIPT_RAW[:]=[{'text':raw,'start':1000,'end':2000,'language':'fr',
                'timestamp':[[1000,2000]],'words':[{'word':raw,'start':1000,'end':2000}]}]
            self.addCleanup(gen.TRANSCRIPT_RAW.clear)
            def persist(sources,dest,*args,**kwargs):
                dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes(b'audio')
            stack.enter_context(patch.object(gen.transcript_lib,'persist_mix',side_effect=persist))
            stack.enter_context(patch.dict(os.environ,{'SUBTITLE_NO_TRANSCRIPT':''}))
            gen.write_transcript_json(output,[{'text':raw,'start':1000,'end':2000}],[],[],'sequential')
            data=json.loads((root/'talk.transcript.json').read_text())
            cue=data['cues'][0]
            self.assertEqual(cue['text'],raw)
            self.assertEqual(cue['chars'][3],cue['chars'][4])
            self.assertEqual(cue['chars'][0][0],1000)
            self.assertEqual(cue['chars'][-1][1],2000)
            self.assertFalse(cue.get('approx'))

    def test_batch_uncertainty_is_preserved(self):
        cue={'text':'café?','start':1000,'end':2000,'language':'fr'}
        with patch.object(gen,'run_agent_task',return_value=[{'id':0,'text':'rewritten','uncertain':'unclear word'}]):
            kept, _, uncertain=gen.polish_cues([cue],True,'',[],100,4000,mode='batch')
        self.assertEqual(kept[0]['text'],'café?')
        self.assertEqual(uncertain[0]['text'],'café?')

    def test_media_pipeline_keeps_source_and_translation_resumes_without_asr(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root=Path(directory)
            source=root/'recording.wav';source.write_bytes(b'audio')
            output=root/'original.srt'
            raw='Café déjà vu. Привет!'
            router=Mock()
            router.records=[{'engine':'whisper','audio_language':'auto','model':'small'}]
            router.transcribe.return_value=[{'text':raw,'start':1000,'end':4000,'language':'fr'}]
            stack.enter_context(patch.object(gen,'ASRRouter',return_value=router))
            stack.enter_context(patch.object(gen,'sort_media',return_value=([source],'name')))
            stack.enter_context(patch.object(gen,'media_duration',return_value=5))
            stack.enter_context(patch.object(gen,'dual_channel_report',return_value=None))
            stack.enter_context(patch.object(gen,'polish_cues',side_effect=lambda cues,*args,**kwargs:(cues,0,[])))
            stack.enter_context(patch.object(gen,'write_transcript_json'))
            stack.enter_context(patch.dict(os.environ,{'SUBTITLE_LLM':'1','SUBTITLE_HANDOFF_DIR':str(root/'tasks')}))
            stack.enter_context(patch.object(sys,'argv',['generate_subtitle.py',str(source),'-o',str(output),'--translate-to','en','--device','cpu']))
            buffer=stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            llm._pending.clear()
            self.addCleanup(llm._pending.clear)
            self.assertEqual(gen.main(),10)
            original=output.read_bytes()
            parsed=gen.parse_text(original.decode())
            self.assertEqual(' '.join(text for _,text in parsed),raw)
            self.assertEqual(json.loads(output.with_suffix('.manifest.json').read_text())['audio_language'],'auto')
            self.assertIn('translate_subtitle.py',buffer.getvalue())
            tasks=list((root/'tasks').glob('*/in.json'))
            self.assertEqual(len(tasks),1)
            data=json.loads(tasks[0].read_text())['cues']
            tasks[0].with_name('out.json').write_text(json.dumps([{'id':c['id'],'text':c['text']} for c in data]))
            self.assertEqual(translation.translate_file(output,'en'),0)
            self.assertEqual(output.read_bytes(),original)
            self.assertEqual(router.transcribe.call_count,1)
            after=gen.parse_text((root/'original.en.srt').read_text())
            self.assertEqual([x[0] for x in parsed],[x[0] for x in after])


if __name__=='__main__': unittest.main()
