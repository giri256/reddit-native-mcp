import os
import tempfile

# Never read or write the real ~/.config/reddit-native-mcp/session.json.
os.environ["REDDIT_NATIVE_MCP_HOME"] = tempfile.mkdtemp(prefix="reddit-native-mcp-test-")
