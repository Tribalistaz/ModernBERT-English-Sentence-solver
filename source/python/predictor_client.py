"""
Sentence Rebuilder - Socket Client
Connects to the server and sends prediction requests
This is called by PHP instead of the full predictor.py
"""

#import libraries
import socket
import json
import sys

# Server configuration 
HOST = '127.0.0.1'
PORT = 65432
TIMEOUT = 120  # latency for timeout

def send_request(sentence, mode='best'):
    """Send prediction request to server and get response"""
    try:
        #create socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
            # Set timeout
            client_socket.settimeout(TIMEOUT)
            
            #connect to server
            client_socket.connect((HOST, PORT))
            
            # prepare request
            request = {
                'sentence': sentence,
                'mode': mode
            }
            
            # Send request
            request_json = json.dumps(request).encode('utf-8')
            client_socket.sendall(request_json)
            
            # Receive response
            response_data = b''
            while True:
                chunk = client_socket.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                #prase as JSON output
                try:
                    json.loads(response_data.decode('utf-8'))
                    break  # Complete response received
                except json.JSONDecodeError:
                    continue  # constant receiving
            
            # Parse response
            response = json.loads(response_data.decode('utf-8'))
            
            return response
    
    except socket.timeout:
        return {
            'success': False,
            'error': 'Server timeout - request took too long'
        }
    
    except ConnectionRefusedError:
        return {
            'success': False,
            'error': 'Cannot connect to server - make sure predictor_server.py is running'
        }
    
    except Exception as e:
        return {
            'success': False,
            'error': f'Client error: {str(e)}'
        }
#main function
def main():
    """Main entry point - called by PHP"""
    if len(sys.argv) < 2:
        print(json.dumps({
            'success': False,
            'error': 'No sentence provided',
            'usage': 'python predictor_client.py "Your sentence" [--mode=best|top3]'
        }))
        sys.exit(1)
    
    # parse arguments
    sentence = sys.argv[1]
    mode = 'best'
    
    #check for mode argument either best or top 3
    for arg in sys.argv[2:]:
        if arg.startswith('--mode='):
            mode = arg.split('=')[1].lower()
            if mode not in ['best', 'top3']:
                mode = 'best'
    
    # Send request to server
    response = send_request(sentence, mode)
    
    # Output response as JSON read by PHP
    print(json.dumps(response))

if __name__ == '__main__':
    main()
