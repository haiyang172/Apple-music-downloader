import sys

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

from PySide6.QtCore import QCoreApplication, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

url = open(r"D:\AMDL\handoff\media-url.txt", encoding="utf-8").read().strip()
app = QCoreApplication(sys.argv[:1])
manager = QNetworkAccessManager()
state = {"bytes": 0, "status": None, "error": None}


def finished(reply):
    state["status"] = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    state["error"] = reply.errorString()
    state["bytes"] = len(reply.readAll())
    print(f"status={state['status']} error={state['error']} bytes={state['bytes']}")
    app.quit()


request = QNetworkRequest(QUrl(url))
request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, True)
request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "Mozilla/5.0")
reply = manager.get(request)
reply.finished.connect(lambda: finished(reply))
QTimer.singleShot(30000, app.quit)
app.exec()
