"""CPU-only replay; no files or serving processes are changed."""
import json
from types import SimpleNamespace
from transformers import AutoTokenizer
from vllm.entrypoints.openai.chat_completion.protocol import ChatCompletionRequest
from vllm.sampling_params import SamplingParams
from vllm.tool_parsers.muse_glimmer_tool_parser import MuseGlimmerToolParser
from vllm.reasoning.muse_glimmer_reasoning_parser import MuseGlimmerReasoningParser
from dynamo.frontend.prepost import StreamingPostProcessor

MODEL = '/model/.model-express/cache/models--nvidia--Muse-Glimmer-30B-NVFP4/snapshots/47818374517751c48c55cde2621594926b1888b6'
tok = AutoTokenizer.from_pretrained(MODEL, local_files_only=True, trust_remote_code=True)
class MarkerAwareMuseParser(MuseGlimmerToolParser):
    tool_call_start_token = '<atem:function_calls>'
    # Keep Dynamo's handoff buffer until finish_reason; early flushing replays stale text.

text = (' to=self<|message|>I need to call the tool.<|eom|>'
        '<|start|>assistant to=probe_read<|message|><atem:function_calls>\n'
        '<atem:invoke name="probe_read">\n'
        '<atem:parameter name="key">probe</atem:parameter>\n'
        '</atem:invoke>\n</atem:function_calls><|eom|>')
ids = tok.encode(text, add_special_tokens=False)
results=[]
for parser_cls in [MuseGlimmerToolParser, MarkerAwareMuseParser]:
 for stream in [True,False]:
  for stride in [1,15,len(ids)]:
   request=ChatCompletionRequest(model='muse',messages=[{'role':'user','content':'call probe_read'}],tools=[{'type':'function','function':{'name':'probe_read','parameters':{'type':'object','properties':{'key':{'type':'string'}},'required':['key']}}}],tool_choice='auto',stream=stream)
   parser=parser_cls(tok, request.tools)
   post=StreamingPostProcessor(tokenizer=tok,request_for_sampling=request,sampling_params=SamplingParams(),prompt_token_ids=tok.encode('<|start|>assistant',add_special_tokens=False),tool_parser=parser,reasoning_parser_class=MuseGlimmerReasoningParser,chat_template_kwargs={},stream_response=stream)
   choices=[]; previous=''
   for offset in range(0,len(ids),stride):
    end=min(offset+stride,len(ids)); decoded=tok.decode(ids[:end],skip_special_tokens=False)
    out=SimpleNamespace(index=0,text=decoded[len(previous):],token_ids=ids[offset:end],finish_reason='stop' if end==len(ids) else None,logprobs=None)
    choice=post.process_output(out)
    if choice: choices.append(choice)
    previous=decoded
   calls=[call for ch in choices for call in ch['delta'].get('tool_calls',[])]
   content=''.join(ch['delta'].get('content','') for ch in choices)
   passed=len(calls)==1 and calls[0]['function']['name']=='probe_read' and json.loads(calls[0]['function']['arguments'])=={'key':'probe'} and not content and choices[-1]['finish_reason']=='tool_calls'
   result={'parser':parser_cls.__name__,'stream':stream,'tokens_per_chunk':stride,'markers':post._tool_start_markers(),'tool_calls':calls,'content':content,'finish_reason':choices[-1]['finish_reason'] if choices else None,'passed':passed}
   print(json.dumps(result),flush=True)
   results.append(result)
assert all(r['passed'] for r in results if r['parser']=='MarkerAwareMuseParser')
assert any(not r['passed'] for r in results if r['parser']=='MuseGlimmerToolParser' and r['stream'])
