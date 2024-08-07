#!/usr/bin/env python3
from flask import Flask,render_template,request,g,redirect,Response
import flask.helpers
import requests
import jwt
import sqlite3
from datetime import datetime, timezone
import time
import os,sys,shutil,traceback,logging
from threading import Thread
import json
import uuid
import re

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"version.txt")) as f:
    __version__ = f.read()

# ========== Database ==========

def init_db():
    con = sqlite3.connect(app.config['graph_spy_db_path'])
    con.execute('CREATE TABLE accesstokens (id INTEGER PRIMARY KEY AUTOINCREMENT, stored_at TEXT, issued_at TEXT, expires_at TEXT, description TEXT, user TEXT, resource TEXT, accesstoken TEXT)')
    con.execute('CREATE TABLE refreshtokens (id INTEGER PRIMARY KEY AUTOINCREMENT, stored_at TEXT, description TEXT, user TEXT, tenant_id TEXT, resource TEXT, foci INTEGER, refreshtoken TEXT)')
    con.execute('CREATE TABLE devicecodes (id INTEGER PRIMARY KEY AUTOINCREMENT, generated_at INTEGER, expires_at INTEGER, user_code TEXT, device_code TEXT, interval INTEGER, client_id TEXT, status TEXT, last_poll INTEGER)')
    con.execute('CREATE TABLE request_templates (id INTEGER PRIMARY KEY AUTOINCREMENT, template_name TEXT, uri TEXT, method TEXT, request_type TEXT, body TEXT, headers TEXT, variables TEXT)')
    con.execute('CREATE TABLE teams_settings (access_token_id INTEGER PRIMARY KEY, skypeToken TEXT, skype_id TEXT, issued_at INTEGER, expires_at INTEGER, teams_settings_raw TEXT)')
    con.execute('CREATE TABLE settings (setting TEXT UNIQUE, value TEXT)')
    # Valid Settings: active_access_token_id, active_refresh_token_id, schema_version, user_agent
    cur = con.cursor()
    cur.execute("INSERT INTO settings (setting, value) VALUES ('schema_version', '3')")
    con.commit()
    con.close()

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(app.config['graph_spy_db_path'])
    return db

def query_db(query, args=(), one=False):
    con = get_db()
    con.row_factory = sqlite3.Row
    cur = con.execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv
    
def query_db_json(query, args=(), one=False):
    con = get_db()
    con.row_factory = make_dicts
    cur = con.execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv
    
def execute_db(statement, args=()):
    con = get_db()
    cur = con.cursor()
    cur.execute(statement, args)
    con.commit()
    
def make_dicts(cursor, row):
    return dict((cursor.description[idx][0], value)
                for idx, value in enumerate(row))
    
def list_databases():
    db_folder_content = os.scandir(app.config['graph_spy_db_folder'])
    databases = [
        {
            'name': db_file.name, 
            'last_modified': f"{datetime.fromtimestamp(db_file.stat().st_mtime)}".split(".")[0], 
            'size': f"{round(db_file.stat().st_size/1024)} KB",
            'state': "Active" if db_file.name.lower() == os.path.basename(app.config['graph_spy_db_path']).lower() else "Inactive"
        } for db_file in db_folder_content if db_file.is_file() and db_file.name.endswith(".db")]
    return databases

def update_db():
    latest_schema_version = "3"
    current_schema_version = query_db("SELECT value FROM settings where setting = 'schema_version'",one=True)[0]
    if current_schema_version == "1":
        print("[*] Current database is schema version 1, updating to schema version 2")
        execute_db("CREATE TABLE request_templates (id INTEGER PRIMARY KEY AUTOINCREMENT, template_name TEXT, uri TEXT, method TEXT, request_type TEXT, body TEXT, headers TEXT, variables TEXT)")
        execute_db("UPDATE settings SET value = '2' WHERE setting = 'schema_version'")
        print("[*] Updated database to schema version 2")
    if current_schema_version == "2":
        print("[*] Current database is schema version 2, updating to schema version 3")
        execute_db('CREATE TABLE teams_settings (access_token_id INTEGER PRIMARY KEY, skypeToken TEXT, skype_id TEXT, issued_at INTEGER, expires_at INTEGER, teams_settings_raw TEXT)')
        execute_db("UPDATE settings SET value = '3' WHERE setting = 'schema_version'")
        print("[*] Updated database to schema version 3")

# ========== Helper Functions ==========

def get_user_agent():
    user_agent = query_db("SELECT value FROM settings where setting = 'user_agent'",one=True)
    if user_agent:
        return user_agent[0]
    else:
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

def set_user_agent(user_agent):
    execute_db("INSERT OR REPLACE INTO settings (setting, value) VALUES ('user_agent',?)",(user_agent,))
    if get_user_agent() == user_agent:
        return True
    else:
        return False

def graph_request(graph_uri, access_token_id):
    access_token = query_db("SELECT accesstoken FROM accesstokens where id = ?",[access_token_id],one=True)[0]
    headers = {"Authorization":f"Bearer {access_token}", "User-Agent":get_user_agent()}
    response = requests.get(graph_uri, headers=headers)
    resp_json = response.json()
    return json.dumps(resp_json)

def graph_request_post(graph_uri, access_token_id, body):
    access_token = query_db("SELECT accesstoken FROM accesstokens where id = ?",[access_token_id],one=True)[0]
    headers = {"Authorization":f"Bearer {access_token}", "User-Agent":get_user_agent()}
    response = requests.post(graph_uri, headers=headers, json=body)
    resp_json = response.json()
    return json.dumps(resp_json)

def graph_upload_request(upload_uri, access_token_id, file):
    access_token_entry = query_db("SELECT accesstoken FROM accesstokens WHERE id = ?", [access_token_id], one=True)
    if not access_token_entry:
        return json.dumps({"error": "Invalid access token ID"}), 400

    access_token = access_token_entry[0]
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": file.content_type, "User-Agent":get_user_agent()}

    response = requests.put(upload_uri, headers=headers, data=file.read())

    if response.status_code in [200, 201]:
        return json.dumps({"message": "File uploaded successfully."}), response.status_code
    else:
        return json.dumps({"error": "Failed to upload file.", "details": response.text}), response.status_code
    
def generic_request(uri, access_token_id, method, request_type, body, headers={}, cookies={}):
    access_token = query_db("SELECT accesstoken FROM accesstokens where id = ?",[access_token_id],one=True)[0]
    headers["Authorization"] = f"Bearer {access_token}"
    headers["User-Agent"] = get_user_agent()

    # Empty body
    if not body:
        response = requests.request(method, uri, headers=headers)
    # Text, XML or urlencoded request
    elif request_type in ["text", "urlencoded", "xml"]:
        if request_type == "urlencoded" and not "Content-Type" in headers:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if request_type == "xml" and not "Content-Type" in headers:
            headers["Content-Type"] = "application/xml"
        response = requests.request(method, uri, headers=headers, data=body)
    # Json request
    elif request_type == "json":
        try:
            response = requests.request(method, uri, headers=headers, json=json.loads(body))
        except ValueError as e:
            return f"[Error] The body message does not contain valid JSON, but a body type of JSON was specified.", 400
    else:
        return f"[Error] Invalid request type.", 400

    # Format json if the Content-Type contains json
    response_type = "json" if ("Content-Type" in response.headers and "json" in response.headers["Content-Type"]) else "xml" if ("Content-Type" in response.headers and "xml" in response.headers["Content-Type"]) else "text"
    try:
        response_text = json.dumps(response.json()) if response_type == "json" else response.text
    except ValueError as e:
        response_text = response.text
    return {"response_status_code": response.status_code ,"response_type": response_type ,"response_text": response_text, "response_headers": dict(response.headers)}

def save_access_token(accesstoken, description):
    decoded_accesstoken = jwt.decode(accesstoken, options={"verify_signature": False})
    user = "unknown"
    # If the idtype is user, use the unique_name or upn
    # If the idtype is app, use the app_displayname or appid
    # Otherwise, use whatever we can get
    if "idtyp" in decoded_accesstoken and decoded_accesstoken["idtyp"] == "user":
        user = decoded_accesstoken["unique_name"] if "unique_name" in decoded_accesstoken else decoded_accesstoken["upn"] if "upn" in decoded_accesstoken else "unknown"
    elif "idtyp" in decoded_accesstoken and decoded_accesstoken["idtyp"] == "app":
        user = decoded_accesstoken["app_displayname"] if "app_displayname" in decoded_accesstoken else decoded_accesstoken["appid"] if "appid" in decoded_accesstoken else "unknown"
    else:
        user = decoded_accesstoken["unique_name"] if "unique_name" in decoded_accesstoken \
            else decoded_accesstoken["upn"] if "upn" in decoded_accesstoken \
            else decoded_accesstoken["app_displayname"] if "app_displayname" in decoded_accesstoken \
            else decoded_accesstoken["oid"] if "oid" in decoded_accesstoken \
            else "unknown"
    
    execute_db("INSERT INTO accesstokens (stored_at, issued_at, expires_at, description, user, resource, accesstoken) VALUES (?,?,?,?,?,?,?)",(
            f"{datetime.now()}".split(".")[0],
            datetime.fromtimestamp(decoded_accesstoken["iat"]) if "iat" in decoded_accesstoken else "unknown",
            datetime.fromtimestamp(decoded_accesstoken["exp"]) if "exp" in decoded_accesstoken else "unknown",
            description,
            user,
            decoded_accesstoken["aud"] if "aud" in decoded_accesstoken else "unknown",
            accesstoken
            )
    )
    
def save_refresh_token(refreshtoken, description, user, tenant, resource, foci):
    # Used to convert potential boolean inputs to an integer, as the DB uses an integer to store this value
    foci_int = 1 if foci else 0
    tenant_id = tenant.strip('"{}-[]\\/\' ') if is_valid_uuid(tenant.strip('"{}-[]\\/\' ')) else get_tenant_id(tenant)
    execute_db("INSERT INTO refreshtokens (stored_at, description, user, tenant_id, resource, foci, refreshtoken) VALUES (?,?,?,?,?,?,?)",(
            f"{datetime.now()}".split(".")[0],
            description,
            user,
            tenant_id,
            resource,
            foci_int,
            refreshtoken
            )
    )

def is_valid_uuid(val):
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False

def get_tenant_id(tenant_domain):
    headers = {"User-Agent":get_user_agent()}
    response = requests.get(f"https://login.microsoftonline.com/{tenant_domain}/.well-known/openid-configuration", headers=headers)
    resp_json = response.json()
    tenant_id = resp_json["authorization_endpoint"].split("/")[3]
    return tenant_id

def refresh_to_access_token(refresh_token_id, resource = "defined_in_token", client_id = "d3590ed6-52b3-4102-aeff-aad2292ab01c", store_refresh_token = True):
    refresh_token = query_db("SELECT refreshtoken FROM refreshtokens where id = ?",[refresh_token_id],one=True)[0]
    tenant_id = query_db("SELECT tenant_id FROM refreshtokens where id = ?",[refresh_token_id],one=True)[0]
    resource = query_db("SELECT resource FROM refreshtokens where id = ?",[refresh_token_id],one=True)[0] if resource == "defined_in_token" else resource
    body =  {
        "resource": resource,
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "openid"
    }
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/token?api-version=1.0"
    headers = {"User-Agent":get_user_agent()}
    response = requests.post(url, data=body, headers=headers)
    access_token = response.json()["access_token"]
    save_access_token(access_token, f"Created using refresh token {refresh_token_id}")
    access_token_id = query_db("SELECT id FROM accesstokens where accesstoken = ?",[access_token],one=True)[0]
    if store_refresh_token:
        decoded_accesstoken = jwt.decode(access_token, options={"verify_signature": False})
        user = "unknown"
        # If the idtype is user, use the unique_name or upn
        # If the idtype is app, use the app_displayname or appid
        if "idtyp" in decoded_accesstoken and decoded_accesstoken["idtyp"] == "user":
            user = decoded_accesstoken["unique_name"] if "unique_name" in decoded_accesstoken else decoded_accesstoken["upn"] if "upn" in decoded_accesstoken else "unknown"
        elif "idtyp" in decoded_accesstoken and decoded_accesstoken["idtyp"] == "app":
            user = decoded_accesstoken["app_displayname"] if "app_displayname" in decoded_accesstoken else decoded_accesstoken["appid"] if "appid" in decoded_accesstoken else "unknown"
        save_refresh_token(
            response.json()["refresh_token"],
            f"Created using refresh token {refresh_token_id}",
            user,
            tenant_id,
            response.json()["resource"]  if "resource" in response.json() else "unknown",
            response.json()["foci"] if "foci" in response.json() else 0
        )
    return access_token_id

# ========== Device Code Functions ==========

def generate_device_code(resource = "https://graph.microsoft.com", client_id = "d3590ed6-52b3-4102-aeff-aad2292ab01c"):
    body =  {
        "resource": resource,
        "client_id": client_id
    }
    url = "https://login.microsoftonline.com/common/oauth2/devicecode?api-version=1.0"
    headers = {"User-Agent":get_user_agent()}
    response = requests.post(url, data=body,headers=headers)

    execute_db("INSERT INTO devicecodes (generated_at, expires_at, user_code, device_code, interval, client_id, status, last_poll) VALUES (?,?,?,?,?,?,?,?)",(
            int(datetime.now().timestamp()),
            int(datetime.now().timestamp()) + int(response.json()["expires_in"]),
            response.json()["user_code"],
            response.json()["device_code"],
            int(response.json()["interval"]),
            client_id,
            "CREATED",
            0
        )
    )
    return response.json()["device_code"]

def poll_device_codes():
    with app.app_context():
        while True:
            rows = query_db_json("SELECT * FROM devicecodes WHERE status IN ('CREATED','POLLING')")
            if not rows:
                return
            sorted_rows =  sorted(rows, key=lambda x: x["last_poll"])
            for row in sorted_rows:
                current_time_seconds = int(datetime.now().timestamp())
                if current_time_seconds > row["expires_at"]:
                    execute_db("UPDATE devicecodes SET status = ? WHERE device_code = ?",("EXPIRED",row["device_code"]))
                    continue
                next_poll = row["last_poll"] + row["interval"]
                #print(f"[{current_time_seconds}] {row['user_code']} - {row['last_poll']} - {next_poll}", flush=True)
                if current_time_seconds < next_poll:
                    time.sleep(next_poll - current_time_seconds)
                if row["status"] == "CREATED":
                    execute_db("UPDATE devicecodes SET status = ? WHERE device_code = ?",("POLLING",row["device_code"]))
                body = {
                    "client_id": row["client_id"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "code": row["device_code"]
                }
                url = "https://login.microsoftonline.com/Common/oauth2/token?api-version=1.0"
                headers = {"User-Agent":get_user_agent()}
                response = requests.post(url, data=body, headers=headers)
                execute_db("UPDATE devicecodes SET last_poll = ? WHERE device_code = ?",(int(datetime.now().timestamp()),row["device_code"]))
                if response.status_code == 200 and "access_token" in response.json():
                    access_token = response.json()["access_token"]
                    user_code = row["user_code"]
                    save_access_token(access_token, f"Created using device code auth ({user_code})")
                    decoded_accesstoken = jwt.decode(access_token, options={"verify_signature": False})
                    user = "unknown"
                    # If the idtype is user, use the unique_name or upn
                    # If the idtype is app, use the app_displayname or appid
                    if "idtyp" in decoded_accesstoken and decoded_accesstoken["idtyp"] == "user":
                        user = decoded_accesstoken["unique_name"] if "unique_name" in decoded_accesstoken else decoded_accesstoken["upn"] if "upn" in decoded_accesstoken else "unknown"
                    elif "idtyp" in decoded_accesstoken and decoded_accesstoken["idtyp"] == "app":
                        user = decoded_accesstoken["app_displayname"] if "app_displayname" in decoded_accesstoken else decoded_accesstoken["appid"] if "appid" in decoded_accesstoken else "unknown"
                    save_refresh_token(
                        response.json()["refresh_token"], 
                        f"Created using device code auth ({user_code})", 
                        user, 
                        decoded_accesstoken["tid"] if "tid" in decoded_accesstoken else "unknown", 
                        response.json()["resource"]if "resource" in response.json() else "unknown", 
                        int(response.json()["foci"])) if "foci" in response.json() else 0
                    execute_db("UPDATE devicecodes SET status = ? WHERE device_code = ?",("SUCCESS",row["device_code"]))

def start_device_code_thread():
    if "device_code_thread" in app.config:
        if app.config["device_code_thread"].is_alive():
            return "[Error] Device Code polling thread is still running."
    app.config["device_code_thread"] =  Thread(target=poll_device_codes)
    app.config["device_code_thread"].start()
    return "[Success] Started device code polling thread."

def device_code_flow(resource = "https://graph.microsoft.com", client_id = "d3590ed6-52b3-4102-aeff-aad2292ab01c"):
    device_code = generate_device_code(resource, client_id)
    row = query_db_json("SELECT * FROM devicecodes WHERE device_code = ?",[device_code],one=True)
    user_code = row["user_code"]
    start_device_code_thread()
    return user_code

# ========== Teams Functions ==========

def getTeamsSettings(access_token_id):
    # If there are skype settings in the DB already that matches the access_token_id, and it is not expired yet, return those
    teams_settings_db = query_db_json("SELECT * FROM teams_settings WHERE access_token_id = ?",[access_token_id],one=True)
    if teams_settings_db and int(datetime.now().timestamp()) < teams_settings_db["expires_at"]:
        gspy_log.debug("Found teams settings in database. Using those.")
        return teams_settings_db
    # Else, request a new skype token, store it in the DB, and return that
    gspy_log.debug(f"No teams settings found in database for access token with ID {access_token_id}. Requesting new teams settings.")
    access_token = query_db("SELECT accesstoken FROM accesstokens WHERE id = ? AND resource LIKE '%api.spaces.skype.com%'",[access_token_id],one=True)
    if not access_token:
        gspy_log.error(f"No access token with ID {access_token_id} and resource containing 'api.spaces.skype.com'!")
        return False
    access_token = access_token[0]
    headers = {"Authorization":f"Bearer {access_token}", "User-Agent":get_user_agent()}
    uri = "https://teams.microsoft.com/api/authsvc/v1.0/authz"
    response = requests.post(uri, headers=headers)
    if response.status_code != 200:
        gspy_log.error(f"Failed obtaining teams settings. Received status code {response.status_code}")
        return False
    try:
        teams_settings_json = response.json()
        skype_token = teams_settings_json["tokens"]["skypeToken"]
        decoded_skype_token = jwt.decode(skype_token, options={"verify_signature": False})
        teams_settings_string = json.dumps(teams_settings_json)
        execute_db("INSERT OR REPLACE INTO teams_settings (access_token_id, skypeToken, skype_id, issued_at, expires_at, teams_settings_raw) VALUES (?,?,?,?,?,?)",(
                access_token_id,
                skype_token,
                decoded_skype_token["skypeid"],
                decoded_skype_token["iat"],
                decoded_skype_token["exp"],
                teams_settings_string
                )
            )
        teams_settings_db = query_db_json("SELECT * FROM teams_settings WHERE access_token_id = ?",[access_token_id],one=True)
        if teams_settings_db:
            return teams_settings_db
    except Exception as e:
        gspy_log.error(f"Failed extracting teams settings from response.")
        traceback.print_exc()
    return False

def safe_join(directory, filename):
    # Safely join `directory` and `filename`.
    os_seps = list(sep for sep in [os.path.sep, os.path.altsep] if sep != None)
    filename = os.path.normpath(filename)
    for sep in os_seps:
        if sep in filename:
            return False
    if os.path.isabs(filename) or filename.startswith('../'):
        return False
    if not os.path.normpath(os.path.join(directory,filename)).startswith(directory):
        return False
    return os.path.join(directory, filename)

def init_routes():

    # ========== Pages ==========

    @app.route("/")
    def settings():
        return render_template('settings.html', title="Settings")

    @app.route("/access_tokens")
    def access_tokens():
        return render_template('access_tokens.html', title="Access Tokens")

    @app.route("/refresh_tokens")
    def refresh_tokens():
        return render_template('refresh_tokens.html', title="Refresh Tokens")

    @app.route("/device_codes")
    def device_codes():
        return render_template('device_codes.html', title="Device Codes")

    @app.route("/custom_requests")
    def custom_requests():
        return render_template('custom_requests.html', title="Custom Requests")

    @app.route("/generic_search")
    def generic_search():
        return render_template('generic_search.html', title="Generic MSGraph Search")

    @app.route("/recent_files")
    def recent_files():
        return render_template('recent_files.html', title="Recent Files")

    @app.route("/shared_with_me")
    def shared_with_me():
        return render_template('shared_with_me.html', title="Files Shared With Me")

    @app.route("/onedrive")
    def onedrive():
        return render_template('OneDrive.html', title="OneDrive")

    @app.route("/sharepoint_sites")
    def sharepoint_sites():
        return render_template('SharePointSites.html', title="SharePoint Sites")
    
    @app.route("/sharepoint_drives")
    def sharepoint_drives():
        return render_template('SharePointDrives.html', title="SharePoint Drives")
    
    @app.route("/sharepoint")
    def sharepoint():
        return render_template('SharePoint.html', title="SharePoint")
        
    @app.route("/outlook")
    def outlook():
        return render_template('outlook.html', title="Outlook")

    @app.route("/teams")
    def teams():
        return render_template('teams.html', title="Microsoft Teams")

    # ========== API ==========

        # ========== Device Codes ==========

    @app.route("/api/list_device_codes")
    def api_list_device_codes():
        rows = query_db_json("select * from devicecodes")
        # Convert unix timestamps to formated datetime strings before returning
        [row.update(generated_at=f"{datetime.fromtimestamp(row['generated_at'])}") for row in rows]
        [row.update(expires_at=f"{datetime.fromtimestamp(row['expires_at'])}") for row in rows]
        [row.update(last_poll=f"{datetime.fromtimestamp(row['last_poll'])}") for row in rows]
        return json.dumps(rows)

    @app.post("/api/restart_device_code_polling")
    def api_restart_device_code_polling():
        return start_device_code_thread()

    @app.post('/api/generate_device_code')
    def api_generate_device_code():
        resource = request.form['resource'] if "resource" in request.form and request.form['resource'] else "https://graph.microsoft.com"
        client_id = request.form['client_id'] if "client_id" in request.form and request.form['client_id'] else "d3590ed6-52b3-4102-aeff-aad2292ab01c"
        if resource and client_id:
            user_code = device_code_flow(resource, client_id)
        else:
            user_code = "000000000"
        return user_code

    @app.route("/api/delete_device_code/<id>")
    def api_delete_device_code(id):
        execute_db("DELETE FROM devicecodes where id = ?",[id])
        return "true"
    
        # ========== Refresh Tokens ==========

    @app.route("/api/list_refresh_tokens")
    def api_list_refresh_tokens():
        rows = query_db_json("select * from refreshtokens")
        return json.dumps(rows)

    @app.route("/api/get_refresh_token/<id>")
    def api_get_refresh_token(id):
        rows = query_db_json("select * from refreshtokens WHERE id = ?",[id],one=True)
        return json.dumps(rows)

    @app.post('/api/add_refresh_token')
    def api_add_refresh_token():
        refreshtoken = request.form['refreshtoken'] if "refreshtoken" in request.form else ""
        user = request.form['user'] if "user" in request.form else ""
        tenant = request.form['tenant_domain'] if "tenant_domain" in request.form else ""
        resource = request.form['resource'] if "resource" in request.form else ""
        description = request.form['description'] if "description" in request.form else ""
        foci = 1 if "foci" in request.form else 0
        if refreshtoken and tenant and resource:
            save_refresh_token(refreshtoken, description, user, tenant, resource, foci)
        return redirect('/refresh_tokens')

    @app.post('/api/refresh_to_access_token')
    def api_refresh_to_access_token():
        refresh_token_id = request.form['refresh_token_id'] if "refresh_token_id" in request.form else ""
        resource = request.form['resource'] if "resource" in request.form else ""
        resource = resource if resource else "defined_in_token"
        client_id = request.form['client_id'] if "client_id" in request.form else ""
        client_id = client_id if client_id else "d3590ed6-52b3-4102-aeff-aad2292ab01c"
        store_refresh_token = True if "store_refresh_token" in request.form else False
        access_token_id = 0
        if refresh_token_id and resource and client_id:
            access_token_id = refresh_to_access_token(refresh_token_id, resource, client_id, store_refresh_token)
        return f"{access_token_id}"

    @app.route("/api/delete_refresh_token/<id>")
    def api_delete_refresh_token(id):
        execute_db("DELETE FROM refreshtokens where id = ?",[id])
        return "true"

    @app.route("/api/active_refresh_token/<id>")
    def api_set_active_refresh_token(id):
        previous_id = query_db("SELECT value FROM settings WHERE setting = 'active_refresh_token_id'",one=True)
        if not previous_id:
            execute_db("INSERT INTO settings (setting, value) VALUES ('active_refresh_token_id',?)",(id,))
        else:
            execute_db("UPDATE settings SET value = ? WHERE setting = 'active_refresh_token_id'",(id,))
        return id

    @app.route("/api/active_refresh_token")
    def api_get_active_refresh_token():
        active_refresh_token = query_db("SELECT value FROM settings WHERE setting = 'active_refresh_token_id'",one=True)
        return f"{active_refresh_token[0]}" if active_refresh_token else "0"

        # ========== Access Tokens ==========

    @app.route("/api/list_access_tokens")
    def api_list_access_tokens():
        rows = query_db_json("select * from accesstokens")
        return json.dumps(rows)
    
    @app.post('/api/add_access_token')
    def api_add_access_token():
        accesstoken = request.form['accesstoken'] if "accesstoken" in request.form and request.form['accesstoken'] else ""
        description = request.form['description'] if "description" in request.form else ""
        if accesstoken:
            save_access_token(accesstoken, description)
        return redirect('/access_tokens')

    @app.route("/api/get_access_token/<id>")
    def api_get_access_token(id):
        rows = query_db_json("select * from accesstokens WHERE id = ?",[id],one=True)
        return json.dumps(rows)

    @app.route("/api/decode_token/<id>")
    def api_decode_token(id):
        rows = query_db("SELECT accesstoken FROM accesstokens WHERE id = ?",[id],one=True)
        if rows:
            decoded_accesstoken = jwt.decode(rows[0], options={"verify_signature": False})
            return decoded_accesstoken
        else:
            return f"[Error] Could not find access token with id {id}"

    @app.route("/api/delete_access_token/<id>")
    def api_delete_access_token(id):
        execute_db("DELETE FROM accesstokens WHERE id = ?",[id])
        return "true"

    @app.route("/api/active_access_token/<id>")
    def api_set_active_access_token(id):
        previous_id = query_db("SELECT value FROM settings WHERE setting = 'active_access_token_id'",one=True)
        if not previous_id:
            execute_db("INSERT INTO settings (setting, value) VALUES ('active_access_token_id',?)",(id,))
        else:
            execute_db("UPDATE settings SET value = ? WHERE setting = 'active_access_token_id'",(id,))
        return id

    @app.route("/api/active_access_token")
    def api_get_active_access_token():
        active_access_token = query_db("SELECT value FROM settings WHERE setting = 'active_access_token_id'",one=True)
        return f"{active_access_token[0]}" if active_access_token else "0"
    
        # ========== Generic Requests ==========

    @app.post("/api/generic_graph")
    def api_generic_graph():
        graph_uri = request.form['graph_uri']
        access_token_id = request.form['access_token_id']
        graph_response = graph_request(graph_uri, access_token_id)
        return graph_response
    
    @app.post("/api/generic_graph_post")
    def api_generic_graph_post():
        graph_uri = request.form['graph_uri']
        access_token_id = request.form['access_token_id']
        body = json.loads(request.form['body'])
        graph_response = graph_request_post(graph_uri, access_token_id, body)
        return graph_response
    
    @app.route('/api/generic_graph_upload', methods=['POST'])
    def api_generic_graph_upload():
        try:
            upload_uri = request.form['upload_uri']
            access_token_id = request.form['access_token_id']
            file = request.files['file']

            if not upload_uri or not access_token_id or not file:
                return json.dumps({"error": "Missing required parameters"}), 400

            return graph_upload_request(upload_uri, access_token_id, file)
        except Exception as e:
            print(f"An error occurred: {e}")
            return json.dumps({"error": "An internal server error occurred.", "details": str(e)}), 500

    @app.post("/api/custom_api_request")
    def api_custom_api_request():
        if not request.is_json:
            return f"[Error] Expecting JSON input.", 400
        request_json = request.get_json()
        uri = request_json['uri'] if 'uri' in request_json else ''
        access_token_id = request_json['access_token_id'] if 'access_token_id' in request_json else 0
        method = request_json['method'] if 'method' in request_json else 'GET'
        request_type = request_json['request_type'] if 'request_type' in request_json else 'text'
        body = request_json['body'] if 'body' in request_json else ''
        headers = request_json['headers'] if 'headers' in request_json else {}
        variables = request_json['variables'] if 'variables' in request_json else {}

        if not (uri and access_token_id and method):
            return f"[Error] URI, Access Token ID and Method are mandatory!", 400
        elif request_type not in ["text", "json", "urlencoded", "xml"]:
            return f"[Error] Invalid request type '{request_type}'. Should be one of the following values: text, json, urlencoded, xml", 400
        elif type(headers) != dict or type(variables) != dict:
            return f"[Error] Expecting json input for headers and variables. Received '{type(headers)}' and '{type(variables)}' respectively.", 400

        for variable_name, variable_value in variables.items():
            uri = uri.replace(variable_name, variable_value)
            body = body.replace(variable_name, variable_value)
            temp_headers = {}
            for header_name, header_value in headers.items():
                new_header_name = header_name.replace(variable_name, variable_value) if type(header_name) == str else header_name
                new_header_value = header_value.replace(variable_name, variable_value) if type(header_value) == str else header_value
                temp_headers[new_header_name] =new_header_value
            headers = temp_headers
        try:
            api_response = generic_request(uri, access_token_id, method, request_type, body, headers)
        except Exception as e:
            traceback.print_exc()
            return f"[Error] Unexpected error occurred. Check your input for any issues. Exception: {repr(e)}", 400
        return api_response
        
    @app.post("/api/save_request_template")
    def api_save_request_template():
        if not request.is_json:
            return f"[Error] Expecting JSON input.", 400
        request_json = request.get_json()
        template_name = request_json['template_name'] if 'template_name' in request_json else ''
        uri = request_json['uri'] if 'uri' in request_json else ''
        method = request_json['method'] if 'method' in request_json else 'GET'
        request_type = request_json['request_type'] if 'request_type' in request_json else 'text'
        body = request_json['body'] if 'body' in request_json else ''
        headers = request_json['headers'] if 'headers' in request_json else {}
        variables = request_json['variables'] if 'variables' in request_json else {}

        if not (template_name and uri and method):
            return f"[Error] Template Name, URI and Method are mandatory!", 400
        elif request_type not in ["text", "json", "urlencoded", "xml"]:
            return f"[Error] Invalid request type '{request_type}'. Should be one of the following values: text, json, urlencoded, xml", 400
        elif type(headers) != dict or type(variables) != dict:
            return f"[Error] Expecting json input for headers and variables. Received '{type(headers)}' and '{type(variables)}' respectively.", 400
        
        template_exists = False
        try:
            # If a request template with the same name already exists, delete it first
            existing_request_template = query_db_json("SELECT * FROM request_templates WHERE template_name = ?",[template_name],one=True)
            if existing_request_template:
                template_exists = True
                execute_db("DELETE FROM request_templates where id = ?",[existing_request_template["id"]])
            # Save the new request template
            execute_db("INSERT INTO request_templates (template_name, uri, method, request_type, body, headers, variables) VALUES (?,?,?,?,?,?,?)",(
                template_name,
                uri,
                method,
                request_type,
                body,
                json.dumps(headers),
                json.dumps(variables)
                )
            )
        except Exception as e:
            traceback.print_exc()
            return f"[Error] Unexpected error occurred. Check your input for any issues. Exception: {repr(e)}", 400
        if template_exists:
            return f"[Success] Updated configuration for request template '{template_name}'."
        return f"[Success] Saved template '{template_name}' to database."
    
    @app.route("/api/get_request_templates/<template_id>")
    def api_request_templates(template_id):
        request_template = query_db_json("SELECT * FROM request_templates WHERE id = ?",[template_id],one=True)
        if request_template:
            request_template['headers'] = json.loads(request_template['headers'])
            request_template['variables'] = json.loads(request_template['variables'])
        if not request_template:
            return f"[Error] Unable to find request template with ID '{template_id}'.", 400
        return request_template

    @app.route("/api/list_request_templates")
    def api_list_request_templates():
        request_templates = query_db_json("SELECT * FROM request_templates")
        for i in range(len(request_templates)):
            request_templates[i]['headers'] = json.loads( request_templates[i]['headers'])
            request_templates[i]['variables'] = json.loads(request_templates[i]['variables'])
        return request_templates
    
    @app.post("/api/delete_request_template")
    def api_delete_request_template():
        if not "template_id" in request.form:
            return f"[Error] No template_id specified.", 400
        template_id = request.form['template_id']
        existing_request_template = query_db_json("SELECT * FROM request_templates WHERE id = ?",[template_id],one=True)
        if not existing_request_template:
             return f"[Error] Unable to find request template with ID '{template_id}'.", 400
        execute_db("DELETE FROM request_templates where id = ?",[template_id])
        return f"[Success] Deleted request template '{existing_request_template['template_name']}' from database."

        # ========== Teams ==========

    @app.post("/api/get_teams_conversations")
    def api_get_teams_conversations():
        if not "access_token_id" in request.form:
            return f"[Error] No access_token_id specified.", 400
        access_token_id = request.form['access_token_id']
        teams_settings = getTeamsSettings(access_token_id)
        if not teams_settings:
            return f"[Error] Unable to obtain teams settings with access token {access_token_id}.", 400
        chat_service_uri = json.loads(teams_settings['teams_settings_raw'])['regionGtms']['chatService']
        uri = f"{chat_service_uri}/v1/users/ME/conversations?view=msnp24Equivalent&pageSize=500"
        headers = {"Authentication":f"skypetoken={teams_settings['skypeToken']}"}
        response = generic_request(uri, access_token_id, "GET", "text", "", headers)
        if response['response_status_code'] == 200 and response['response_type'] == "json":
            return json.loads(response['response_text'])
        return f"[Error] Something went wrong trying to obtain Teams Conversations. Received response status {response['response_status_code']} and response type {response['response_type']}", 400

    @app.post("/api/get_teams_conversation_messages")
    def api_get_teams_conversation_messages():
        if not "access_token_id" in request.form:
            return f"[Error] No access_token_id specified.", 400
        access_token_id = request.form['access_token_id']
        if not "conversation_link" in request.form:
            return f"[Error] No conversation_link specified.", 400
        conversation_link = request.form['conversation_link']
        teams_settings = getTeamsSettings(access_token_id)
        if not teams_settings:
            return f"[Error] Unable to obtain teams settings with access token {access_token_id}.", 400
        uri = f"{conversation_link}?startTime=0&view=msnp24Equivalent&pageSize=200"
        headers = {"Authentication":f"skypetoken={teams_settings['skypeToken']}"}
        response = generic_request(uri, access_token_id, "GET", "text", "", headers)
        if response['response_status_code'] == 200 and response['response_type'] == "json":
            conversation_messages = json.loads(response['response_text'])
            # Add `isFromMe: True` to the message if the message is from the current user.
            conversation_messages["messages"] = [{**message, "isFromMe": message["from"].endswith(teams_settings["skype_id"])} for message in conversation_messages["messages"]]
            return conversation_messages
        return f"[Error] Something went wrong trying to obtain Teams Conversations. Received response status {response['response_status_code']} and response type {response['response_type']}", 400

    @app.post("/api/send_teams_conversation_message")
    def api_send_teams_conversation_message():
        if not "access_token_id" in request.form:
            return f"[Error] No access_token_id specified.", 400
        access_token_id = request.form['access_token_id']
        if not "conversation_link" in request.form:
            return f"[Error] No conversation_link specified.", 400
        conversation_link = request.form['conversation_link']
        if not "message_content" in request.form:
            return f"[Error] No message_content specified.", 400
        message_content = request.form['message_content']
        teams_settings = getTeamsSettings(access_token_id)
        if not teams_settings:
            return f"[Error] Unable to obtain teams settings with access token {access_token_id}.", 400
        headers = {"Authentication":f"skypetoken={teams_settings['skypeToken']}", "User-Agent":get_user_agent()}
        body = {
            "messagetype": "RichText/Html",
            "content": message_content
        }
        response = requests.post(conversation_link, headers=headers, json=body)
        if response.status_code >= 200 and response.status_code < 300:
            message_id = response.json()["OriginalArrivalTime"] if "OriginalArrivalTime" in response.json() else "Unknown"
            return f"{message_id}"
        return f"[Error] Something went wrong trying to send Teams message. Received response status {response.status_code}", 400
        gspy_log.error(f"Failed sending teams message. Received response status {response.status_code}. Response body:\n {response.content}")

    @app.post("/api/get_teams_conversation_members")
    def api_get_teams_conversation_members():
        if not "access_token_id" in request.form:
            return f"[Error] No access_token_id specified.", 400
        access_token_id = request.form['access_token_id']
        if not "conversation_id" in request.form:
            return f"[Error] No conversation_id specified.", 400
        conversation_id = request.form['conversation_id']
        teams_settings = getTeamsSettings(access_token_id)
        if not teams_settings:
            return f"[Error] Unable to obtain teams settings with access token {access_token_id}.", 400
        teams_and_channel_service_uri = json.loads(teams_settings['teams_settings_raw'])['regionGtms']['teamsAndChannelsService']
        uri = f"{teams_and_channel_service_uri}/beta/teams/{conversation_id}/members"
        response = generic_request(uri, access_token_id, "GET", "text", "", {})
        if response['response_status_code'] == 200 and response['response_type'] == "json":
            conversation_members = json.loads(response['response_text'])
            # Add `isCurrentUser: True` to the member if the member is the current user.
            conversation_members = [{**member, "isCurrentUser": member["mri"].endswith(teams_settings["skype_id"])} for member in conversation_members]
            gspy_log.debug(f"Found {len(conversation_members)} members in conversation '{conversation_id}'")
            return conversation_members
        gspy_log.error(f"Failed listing members in conversation '{conversation_id}'. Received response status {response['response_status_code']}. Response body: \n{response['response_text']}")
        return f"[Error] Something went wrong trying to obtain Teams Members. Received response status {response['response_status_code']} and response type {response['response_type']}", 400

    @app.get("/api/get_teams_image")
    def api_get_teams_image():
        if not "access_token_id" in request.args:
            return f"[Error] No access_token_id specified.", 400
        access_token_id = request.args['access_token_id']
        if not "image_uri" in request.args:
            return f"[Error] No image_uri specified.", 400
        image_uri = request.args['image_uri']
        teams_settings = getTeamsSettings(access_token_id)
        if not teams_settings:
            return f"[Error] Unable to obtain teams settings with access token {access_token_id}.", 400
        cookies = {"skypetoken_asm":teams_settings['skypeToken']}
        headers = {"User-Agent":get_user_agent()}
        response = requests.get(image_uri, cookies=cookies, headers=headers)
        if response.status_code == 200:
            return Response(response.content, mimetype=response.headers['Content-Type'])
        return f"[Error] Something went wrong trying to obtain teams image. Received response status {response.status_code} and response type {response.headers['Content-Type'] if 'Content-Type' in response.headers else 'empty'}", 400
        
    @app.post("/api/list_teams_users")
    def api_list_teams_users():
        if not "access_token_id" in request.form:
            return f"[Error] No access_token_id specified.", 400
        access_token_id = request.form['access_token_id']
        teams_settings = getTeamsSettings(access_token_id)
        if not teams_settings:
            return f"[Error] Unable to obtain teams settings with access token {access_token_id}.", 400
        teams_and_channel_service_uri = json.loads(teams_settings['teams_settings_raw'])['regionGtms']['teamsAndChannelsService']
        base_uri = f"{teams_and_channel_service_uri}/beta/users?top=999"
        teams_users = []
        next_skiptoken = ""
        while True:
            uri = f"{base_uri}&skipToken={next_skiptoken}" if next_skiptoken else base_uri
            response = generic_request(uri, access_token_id, "GET", "text", "")
            if not (response['response_status_code'] == 200 and response['response_type'] == "json"):
                break
            responseJson = json.loads(response['response_text'])
            if not "users" in responseJson:
                break
            teams_users += responseJson["users"]
            if not "skipToken" in responseJson:
                return teams_users
            next_skiptoken = responseJson["skipToken"]
        return f"[Error] Something went wrong trying to list Teams Users. Received response status {response['response_status_code']} and response type {response['response_type']}", 400

    @app.get("/api/get_teams_user_details")
    def api_get_teams_user_details():
        if not "access_token_id" in request.args:
            return f"[Error] No access_token_id specified.", 400
        access_token_id = request.args['access_token_id']
        teams_settings = getTeamsSettings(access_token_id)
        if not teams_settings:
            return f"[Error] Unable to obtain teams settings with access token {access_token_id}.", 400
        teams_and_channel_service_uri = json.loads(teams_settings['teams_settings_raw'])['regionGtms']['teamsAndChannelsService']
        if not "user_id" in request.args:
            return f"[Error] No user_id specified. Specify a valid UPN, ObjectID or MRI of the user", 400
        user_id = request.args['user_id']
        uri = f"{teams_and_channel_service_uri}/beta/users/{user_id}"
        headers = {"x-ms-client-version":"27/1.0.0.2020101241"}
        if "external" in request.args and request.args["external"].lower() == "true":
            uri += "/externalsearchv3"
        response = generic_request(uri, access_token_id, "GET", "text", "", headers)
        if response['response_status_code'] == 200 and response['response_type'] == "json":
            return json.loads(response['response_text'])
        elif response['response_status_code'] == 404:
            return f"[Error] User '{user_id} not found'", 404
        return f"[Error] Something went wrong trying to list Teams Users. Received response status {response['response_status_code']} and response type {response['response_type']}", 400

    @app.post("/api/create_teams_conversation")
    def api_create_teams_conversation(): # access_token_id, members, type, topic, message_content
        if not request.is_json:
            return f"[Error] Expecting JSON input.", 400
        request_json = request.get_json()
        if not "access_token_id" in request_json:
            return f"[Error] No access_token_id specified.", 400
        access_token_id = request_json['access_token_id']
        if not "members" in request_json:
            return f"[Error] No members specified.", 400
        members = request_json['members']
        if not "type" in request_json:
            return f"[Error] No conversation type specified.", 400
        conversation_type = request_json['type']
        if not conversation_type in ["direct_message", "group_chat"]:
            return f"[Error] Type needs to be either 'direct_message' or 'group_chat'.", 400
        teams_settings = getTeamsSettings(access_token_id)
        if not teams_settings:
            return f"[Error] Unable to obtain teams settings with access token {access_token_id}.", 400
        chat_service_uri = json.loads(teams_settings['teams_settings_raw'])['regionGtms']['chatService']
        uri = f"{chat_service_uri}/v1/threads"
        headers = {"Authentication":f"skypetoken={teams_settings['skypeToken']}", "User-Agent":get_user_agent()}
        # Adding ourself first
        conversation_members = [{
            "id": f"8:{teams_settings['skype_id']}",
            "role": "Admin"
        }]
        conversation_properties = {
            "threadType": "chat",
            "chatFilesIndexId": "2",
            "fixedRoster": "true",
            "uniquerosterthread": "true" if conversation_type == "direct_message" else "false"
        }
        if "topic" in request_json:
            conversation_properties["topic"] = request_json["topic"]
        created_conversations = []
        if conversation_type == "direct_message":
            for member in members:
                body = {
                    "members": conversation_members[:],
                    "properties": conversation_properties
                }
                body["members"].append({
                    "id": member,
                    "role": "Admin"
                })
                response = requests.post(uri, headers=headers, json=body)
                if response.status_code >= 200 and response.status_code < 300 and "Location" in response.headers:
                    conversation_id_regex = re.search('https:\/\/emea\.ng\.msg\.teams\.microsoft\.com\/v1\/threads\/(.*)$', response.headers["Location"])
                    if conversation_id_regex:
                        conversation_id = conversation_id_regex.group(1)
                        created_conversations.append(conversation_id)
                        gspy_log.debug(f"Created conversation with member {member}. Conversation ID: {conversation_id}")
                        continue
                gspy_log.error(f"Failed creating direct message conversation with user {member}. Received response status {response.status_code}.\n{response.content}")
        elif conversation_type == "group_chat":
            for member in members:
                conversation_members.append({
                    "id": member,
                    "role": "Admin"
                })
            body = {
                "members": conversation_members,
                "properties": conversation_properties
            }
            response = requests.post(uri, headers=headers, json=body)
            if response.status_code >= 200 and response.status_code < 300 and "Location" in response.headers:
                conversation_id_regex = re.search('https:\/\/emea\.ng\.msg\.teams\.microsoft\.com\/v1\/threads\/(.*)$', response.headers["Location"])
                if conversation_id_regex:
                    conversation_id = conversation_id_regex.group(1)
                    created_conversations.append(conversation_id)
                    gspy_log.debug(f"Created conversation with {len(members)} members. Conversation ID: {conversation_id}")
                else:
                    gspy_log.error(f"Failed creating group chat conversation. Received response status {response.status_code}\n{response.content}.")
            else:
                gspy_log.error(f"Failed creating group chat conversation. Received response status {response.status_code}.\n{response.content}")
        gspy_log.debug(f"Created {len(created_conversations)} conversations.")
        if len(created_conversations) == 0:
            return f"[Error] Something went wrong creating the conversation(s).", 400
        # If a message is specified, send an initial message to every created conversation
        if "message_content" in request_json:
            body = {
                "messagetype": "RichText/Html",
                "content": request_json["message_content"]
            }
            for conversation_id in created_conversations:
                conversation_link = f"{chat_service_uri}/v1/users/ME/conversations/{conversation_id}/messages"
                response = requests.post(conversation_link, headers=headers, json=body)
                if response.status_code >= 200 and response.status_code < 300:
                    message_id = response.json()["OriginalArrivalTime"] if "OriginalArrivalTime" in response.json() else "Unknown"
                    gspy_log.debug(f"Sent message to conversation {conversation_id}. Message ID: {message_id}")
                else:
                    gspy_log.error(f"Failed sending message to {conversation_id}. Received response status {response.status_code}.\n{response.content}")
        return created_conversations

        # ========== Database ==========
    
    @app.get("/api/list_databases")
    def api_list_databases():
        return list_databases()

    @app.post("/api/create_database")
    def api_create_database():
        database_name = request.form['database']
        if not database_name:
            return f"[Error] Please specify a database name."
        database_name = database_name if database_name.endswith(".db") else f"{database_name}.db"
        db_path = safe_join(app.config['graph_spy_db_folder'],database_name)
        if not db_path:
            return f"[Error] Invalid database name '{database_name}'. Try again with another name."
        if(os.path.exists(db_path)):
            return f"[Error] Database '{database_name}' already exists. Try again with another name."
        old_db = app.config['graph_spy_db_path']
        app.config['graph_spy_db_path'] = db_path
        init_db()
        if(not os.path.exists(db_path)):
            app.config['graph_spy_db_path'] = old_db
            return f"[Error] Failed to create database '{database_name}'."
        return f"[Success] Created and activated '{database_name}'."
    
    @app.post("/api/activate_database")
    def api_activate_database():
        database_name = request.form['database']
        db_path = safe_join(app.config['graph_spy_db_folder'],database_name)
        if(not os.path.exists(db_path)):
            return f"[Error] Database file '{db_path}' not found."
        app.config['graph_spy_db_path'] = db_path
        update_db()
        return f"[Success] Activated database '{database_name}'."
    
    @app.post("/api/duplicate_database")
    def api_duplicate_database():
        database_name = request.form['database']
        db_path = safe_join(app.config['graph_spy_db_folder'],database_name)
        if(not os.path.exists(db_path)):
            return f"[Error] Database file '{db_path}' not found."
        for i in range(1,100):
            new_path = f"{db_path.strip('.db')}_{i}.db"
            if(not os.path.exists(new_path)):
                shutil.copy2(db_path, new_path)
                return f"[Success] Duplicated database '{database_name}' to '{new_path.split('/')[-1]}'."
        return f"[Error] Could not duplicate database '{database_name}'."

    @app.post("/api/delete_database")
    def api_delete_database():
        database_name = request.form['database']
        db_path = safe_join(app.config['graph_spy_db_folder'],database_name)
        if app.config['graph_spy_db_path'].lower() == db_path.lower():
            return "[Error] Can't delete the active database. Select a different database first."
        os.remove(db_path)
        if(not os.path.exists(db_path)):
            return f"[Success] Database '{database_name}' deleted."
        else:
            return f"[Error] Failed to delete '{database_name}' at '{db_path}'."
    
        # ========== Settings ==========
    
    @app.post("/api/set_table_error_messages")
    def api_set_table_error_messages():
        state = request.form['state']
        if state not in ["enabled", "disabled"]:
            return f"[Error] Invalid state '{state}'."
        app.config['table_error_messages'] = state
        return f"[Success] {state.capitalize()} datatable error messages."
    
    @app.get("/api/get_settings")
    def api_get_settings():
        settings_raw = query_db_json("SELECT * FROM settings")
        #settings_json = [{setting["setting"] : setting["value"]} for setting in settings_raw]
        settings_json = {setting["setting"] : setting["value"] for setting in settings_raw}
        return settings_json
        
    @app.get("/api/get_user_agent")
    def api_get_user_agent():
        return get_user_agent()

    @app.post("/api/set_user_agent")
    def api_set_user_agent():
        user_agent = request.form['user_agent'] if "user_agent" in request.form else ""
        if not user_agent:
            return "[Error] User agent not specified!", 400 
        if not set_user_agent(user_agent):
            return f"[Error] Unable to set user agent to '{user_agent}'!", 400 
        return f"[Success] User agent set to '{user_agent}'!" 

    # ========== Other ==========

    @app.teardown_appcontext
    def close_connection(exception):
        db = getattr(g, '_database', None)
        if db is not None:
            db.close()
def main():
    # Banner
    print(fr"""
   ________                             _________
  /       /  by RedByte1337    __      /        /      v{__version__}
 /  _____/___________  ______ |  |__  /   _____/_____ ______ 
/   \  __\_  __ \__  \ \____ \|  |  \ \_____  \\____ \   |  |
\    \_\  \  | \/  __ \|  |_> |   \  \/        \  |_> \___  |
 \______  /__|  |____  |   __/|___|  /_______  /   ___/ ____|
        \/           \/|__|        \/        \/|__|   \/
                """)
    # Argument Parser
    import argparse
    parser = argparse.ArgumentParser(prog="GraphSpy", description="Launches the GraphSpy Flask application", epilog="For more information, see https://github.com/RedByte1337/GraphSpy")
    parser.add_argument("-i","--interface", type=str, help="The interface to bind to. Use 0.0.0.0 for all interfaces. (Default = 127.0.0.1)")
    parser.add_argument("-p", "--port", type=int, help="The port to bind to. (Default = 5000)")
    parser.add_argument("-d","--database", type=str, default="database.db", help="Database file to utilize. (Default = database.db)")
    parser.add_argument("--debug", action="store_true", help="Enable flask debug mode. Will show detailed stack traces when an error occurs.")
    args = parser.parse_args()

    # Configure logging
    global gspy_log
    gspy_log = logging.getLogger(__name__)
    gspy_log.setLevel(logging.DEBUG if args.debug else logging.ERROR)
    log_handler = logging.StreamHandler()
    log_format = "[%(funcName)s():%(lineno)s] %(levelname)s: %(message)s"
    log_handler.setFormatter(logging.Formatter(log_format))
    gspy_log.addHandler(log_handler)

    # Create global Flask app variable
    global app
    app = Flask(__name__)
    init_routes()

    # First time Use
    graph_spy_folder = os.path.normpath(os.path.expanduser("~/.gspy/"))
    if(not os.path.exists(graph_spy_folder)):
        print("[*] First time use detected.")
        print(f"[*] Creating directory '{graph_spy_folder}'.")
        os.mkdir(graph_spy_folder)
        if(not os.path.exists(graph_spy_folder)):
            sys.exit(f"Failed creating directory '{graph_spy_folder}'. Unable to proceed.")
    app.config['graph_spy_folder'] = graph_spy_folder

    # Database
    database = args.database
    # Normalize db path
    database = database if database.endswith(".db") else f"{database}.db"
    # Create database folder if it doesn't exist yet
    graph_spy_db_folder = os.path.normpath(os.path.join(graph_spy_folder,"databases/"))
    if(not os.path.exists(graph_spy_db_folder)):
        print(f"[*] Creating directory '{graph_spy_db_folder}'.")
        os.mkdir(graph_spy_db_folder)
        if(not os.path.exists(graph_spy_db_folder)):
            sys.exit(f"Failed creating directory '{graph_spy_db_folder}'. Unable to proceed.")
    app.config['graph_spy_db_folder'] = graph_spy_db_folder
    graph_spy_db_path = safe_join(graph_spy_db_folder,database)
    if not graph_spy_db_path:
        sys.exit(f"Invalid database name '{database}'.")
    app.config['graph_spy_db_path'] = graph_spy_db_path
    # Initialize DB if it doesn't exist yet
    if(not os.path.exists(graph_spy_db_path)):
        print(f"[*] Database file '{graph_spy_db_path}' not found. Initializing new database.")
        init_db()
    if(not os.path.exists(graph_spy_db_path)):
        sys.exit(f"Failed creating database file at '{graph_spy_db_path}'. Unable to proceed.")
    print(f"[*] Utilizing database '{graph_spy_db_path}'.")
    # Update the database to the latest schema version if required
    with app.app_context():
        update_db()
    # Disable datatable error messages by default.
    app.config['table_error_messages'] = "disabled"
    # Run flask
    print(f"[*] Starting GraphSpy. Open in your browser by going to the url displayed below.\n")
    app.run(debug=args.debug, host=args.interface, port=args.port)

if __name__ == '__main__':
    main()