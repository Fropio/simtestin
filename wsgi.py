import os
import sys
from app import app as application, init_db

# Инициализация БД
init_db()

path = os.path.dirname(os.path.abspath(__file__))
if path not in sys.path:
    sys.path.append(path)

if __name__ == "__main__":
    application.run()