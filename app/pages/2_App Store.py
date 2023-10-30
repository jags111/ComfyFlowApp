from loguru import logger
import json
import streamlit as st
from modules.page import page_init
from modules import get_sqlite_instance
from streamlit_extras.row import row
from threading import Thread
from modules.sqlitehelper import AppStatus
import queue
from modules.download import download_model, get_local_model_file

def bytes_to_human_readable(size_in_bytes, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            break
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.{decimal_places}f} {unit}"

class ProgressEventState():
    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"
    def __init__(self, app_id, info, state) -> None:
        self.app_id = app_id
        self.info = info
        self.state = state

class InstallThread(Thread):
    def __init__(self, app, queue):
        super(InstallThread, self).__init__()
        self.app_id = app["id"]
        self.app_name = app["name"]
        self.app_conf_json = json.loads(app["app_conf"])
        self.api_conf_json = json.loads(app["api_conf"])
        self.queue = queue

    def dispatch_event(self, event):            
            if self.queue is not None:
                logger.debug(f"Dispatch event, {event}")
                self.queue.put(event)
            else:
                logger.info("queue is none")

    def run(self):
        try:
            models_size = 0
            models = []
            logger.debug(f"app_conf_json: {self.app_conf_json}")
            if 'models' in self.app_conf_json:
                node_models = self.app_conf_json['models']
                for node_id in node_models:
                    inputs = node_models[node_id]['inputs']
                    for param in inputs:
                        model_info = inputs[param]
                        models_size += model_info['size']
                        models.append(model_info)
            status_info = f"App {self.app_name} started to download {len(models)} models, total size {bytes_to_human_readable(models_size)}"
            status_event = ProgressEventState(self.app_id, status_info, ProgressEventState.RUNNING)
            self.dispatch_event(status_event)

            # download 
            download_size = 0
            for model in models:
                model_url = model['url']
                model_path = model['path']
                ret = download_model(model_url=model_url, model_path=model_path)
                if ret is None:
                    status_info = f":red[download model from {model_url} to {model_path} failed]"
                    status_event = ProgressEventState(self.app_id, status_info, ProgressEventState.ERROR)
                    self.dispatch_event(status_event)
                    return
                else:
                    download_size += model['size']
                    status_info = f"download model from {model_url} to {model_path}, size {bytes_to_human_readable(model['size'])}, percent {download_size / models_size * 100:.2f}%"
                    status_event = ProgressEventState(self.app_id, status_info, ProgressEventState.RUNNING)
                    self.dispatch_event(status_event)

            status_info = f"App {self.app_name} download finished"
            status_event = ProgressEventState(self.app_id, status_info, ProgressEventState.RUNNING)
            self.dispatch_event(status_event)

            # install, update api_conf
            if 'models' in self.app_conf_json:
                node_models = self.app_conf_json['models']
                for node_id in node_models:
                    inputs = node_models[node_id]['inputs']
                    for param in inputs:
                        model_info = inputs[param]
                        local_model_file = get_local_model_file(model_info['url'])
                        self.api_conf_json[node_id]['inputs'][param] = local_model_file
                logger.debug(f"api_conf_json: {self.api_conf_json}")
                get_sqlite_instance().update_api_conf(self.app_id, json.dumps(self.api_conf_json))

            status_info = f"App {self.app_name} install success"
            status_event = ProgressEventState(self.app_id, status_info, ProgressEventState.COMPLETE)
            self.dispatch_event(status_event)
            logger.info(f"App {self.app_name} installed")
        except Exception as e:
            logger.error(f"Install app error, {e}")
            status_info = f"App {self.app_name} install error, {e}"
            status_event = ProgressEventState(self.app_id, status_info, ProgressEventState.ERROR)
            self.dispatch_event(status_event)


def install_app(app, queue):
    logger.info(f"Start install thread for {app['name']} ...")
    install_thread = InstallThread(app, queue)
    install_thread.start()
    # install_thread.join()
    # logger.info(f"Install thread for {app['name']} finished")
    
def update_install_progress(app, status_queue):
    get_sqlite_instance().update_app_status(app["id"], AppStatus.INSTALLING.value)
    with st.status(f"Waiting for install {app['name']} ...", state="running", expanded=True) as install_progress:
        while True:
            try:
                status_event = status_queue.get()
                logger.debug(f"Got install status event {status_event}")
                info = status_event.info
                state = status_event.state
                if state == ProgressEventState.RUNNING:
                    install_progress.write(info)
                elif state == ProgressEventState.COMPLETE:
                    install_progress.write(info)
                    install_progress.update(label=f"Install app {app['name']} success", state="complete", expanded=True)
                    get_sqlite_instance().update_app_status(app["id"], AppStatus.INSTALLED.value)
                    break
                elif state == ProgressEventState.ERROR:
                    install_progress.write(info)
                    install_progress.update(label=f"Install app {app['name']} error", state="error", expanded=True)
                    get_sqlite_instance().update_app_status(app["id"], AppStatus.ERROR.value)
                    break
            except Exception as e:
                logger.warning(f"Queue get error {e}")
                continue


def show_install_status(app):
    if app["status"] == AppStatus.INSTALLING.value:
        st.info(f"App {app['name']} is installing ...")
    elif app["status"] == AppStatus.INSTALLED.value:
        st.success(f"App {app['name']} is installed")
    elif app["status"] == AppStatus.ERROR.value:
        st.error(f"App {app['name']} install error")

page_init()

def create_app_info_ui(app): 
    app_row = row([1, 6.8, 1.2, 1], vertical_align="bottom")
    try:
        if app["image"] is not None:
            app_row.image(app["image"])
        else:
            app_row.image("public/images/app-150.png")
    except Exception as e:
        logger.error(f"load app image error, {e}")

    # get description limit to 200 chars
    description = app["description"]
    if len(description) > 160:
        description = description[:160] + "..."                
    app_row.markdown(f"""
                    #### {app['name']}
                    {description}
                    """)
            
    app_author = "ComfyFlow"
    app_row.markdown(f"""
                    #### Author
                    {app_author}
                    """)
    
    app_status = app["status"]
    
    if f'{app["id"]}_progress_queue' not in st.session_state:
        status_queue = queue.Queue()
        st.session_state[f'{app["id"]}_progress_queue'] = status_queue
    status_queue = st.session_state.get(f'{app["id"]}_progress_queue')
    if app_status == AppStatus.PUBLISHED.value or app_status == AppStatus.ERROR.value:
        install_button = app_row.button("Install", help="Install app from app store",
                                         key=f"install_{app['id']}",
                                         on_click=install_app, args=(app, status_queue))
        if install_button:
            update_install_progress(app, status_queue)  

    elif app_status == AppStatus.INSTALLING.value or app_status == AppStatus.INSTALLED.value:
        reinstall_button = app_row.button("ReInstall", help="Install app from app store", 
                                          key=f"install_{app['id']}",
                                          on_click=install_app, args=(app, status_queue))
        if reinstall_button:
            update_install_progress(app, status_queue)

with st.container():
    with st.container():
        st.markdown("""
            ### App Store
        """)
        st.markdown("""
            This is a simple app store, you could explore and install apps from here.
        """)

    with st.container():
        apps = get_sqlite_instance().get_all_apps()
        for app in apps:
            st.divider()
            logger.info(f"load app info for {app['name']}")
            create_app_info_ui(app)

            # update app status
            app = get_sqlite_instance().get_app_by_id(app["id"])
            show_install_status(app)

    