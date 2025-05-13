from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return '<h1>I love <a href="https://xixi.com">Xixi baby</a></h1>'


if __name__ == "__main__":
    app.run(debug=True)
