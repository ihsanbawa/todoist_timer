from flask import Flask, jsonify

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    return jsonify({"message": "Webhook received"}), 200

if __name__ == '__main__':
    app.run(debug=True)
