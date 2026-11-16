"""CPU-only container checks; explicitly does not claim GPU inference was tested."""
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys

ROOT=Path('/code')
sys.path.insert(0,str(ROOT/'src'))
import torch
import torchvision
import transformers
import bitsandbytes
from transformers import AutoConfig,AutoTokenizer,BitsAndBytesConfig

assert platform.machine()=='x86_64',platform.machine()
assert torch.__version__.startswith('2.9.1'),torch.__version__
assert torch.version.cuda=='12.8',torch.version.cuda
arch=torch._C._cuda_getArchFlags()
assert 'sm_120' in arch,arch
config=AutoConfig.from_pretrained(ROOT/'verification/model_metadata',local_files_only=True)
tokenizer=AutoTokenizer.from_pretrained(ROOT/'verification/model_metadata',local_files_only=True)
text=tokenizer.apply_chat_template([{'role':'user','content':'A short synthetic prompt.'}],
    tokenize=False,add_generation_prompt=True,enable_thinking=False)
assert isinstance(text,str) and text
spec=importlib.util.spec_from_file_location('local_server',ROOT/'src/serve/local_server.py')
server=importlib.util.module_from_spec(spec);sys.modules[spec.name]=server;spec.loader.exec_module(server)
selection=server.select_model_classes(config.to_dict())
cls=server.resolve_model_cls(selection['model_chain'])
resolved=cls._model_mapping[type(config)] if cls.__name__.startswith('AutoModel') else cls
quant=BitsAndBytesConfig(load_in_4bit=True,bnb_4bit_compute_dtype=torch.float16,
                        bnb_4bit_quant_type='nf4',bnb_4bit_use_double_quant=True)
assert quant.load_in_4bit
subprocess.run([sys.executable,'-m','bizreason.run_v2','--help'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['bash',str(ROOT/'training_code/train.sh')],check=True)
record={'platform':platform.machine(),'torch':torch.__version__,'torchvision':torchvision.__version__,
        'cuda_build':torch.version.cuda,'cuda_arch_flags':arch,'transformers':transformers.__version__,
        'bitsandbytes':bitsandbytes.__version__,'model_type':config.model_type,'model_class':resolved.__name__,
        'selected_loader':cls.__name__,
        'tokenizer_template_offline':True,'fewshot_export_exact':True,'gpu_inference_tested':False}
(ROOT/'verification/container_cpu_checks.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record),flush=True)
