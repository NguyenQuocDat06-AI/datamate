"""Cancel one Kaggle kernel session by id: gen_cancel_session.py <account> <session_id>. Prints only the outcome."""
import sys
sys.path.insert(0, "/home/ubuntu/datamate_review_data")
import gen_kaggle_auth
from kagglesdk.kernels.types.kernels_api_service import ApiCancelKernelSessionRequest
api = gen_kaggle_auth.api(int(sys.argv[1]))
req = ApiCancelKernelSessionRequest()
req.kernel_session_id = int(sys.argv[2])
with api.build_kaggle_client() as kaggle:
    resp = kaggle.kernels.kernels_api_client.cancel_kernel_session(req)
print("cancel requested; error:", getattr(resp, "error_message", None) or "")
