# Tool check — 2026-11-18 19:53

181 tools · PASS 75 · FAIL 0 · KNOWN 1 · wiring-only 108

| tool | mode | result | detail | ms |
|---|---|---|---|---|
| battery_status | run | PASS | reply 62 chars | 37 |
| check_internet | run | PASS | reply 44 chars | 2017 |
| check_permissions | run | PASS | reply 157 chars | 625 |
| convert_units | run | PASS | reply 19 chars | 3 |
| define_word | run | PASS | reply 300 chars | 299 |
| disk_space | run | PASS | reply 32 chars | 3 |
| file_info | run | PASS | reply 70 chars | 3 |
| find_contact | run | PASS | reply 32 chars | 5153 |
| find_file | run | PASS | reply 358 chars | 716 |
| find_image_by_content | run | PASS | reply 54 chars | 1709 |
| form_read | run | PASS | reply 961 chars | 447 |
| get_active_window_tree | run | PASS | reply 4390 chars | 605 |
| get_clipboard | run | PASS | reply 1392 chars | 120 |
| get_news | run | PASS | reply 1405 chars | 3018 |
| get_system_info | run | PASS | reply 214 chars | 69 |
| get_volume | run | PASS | reply 19 chars | 1109 |
| get_weather | run | PASS | reply 79 chars | 7846 |
| ip_address | run | PASS | reply 39 chars | 197 |
| list_agenda | run | PASS | reply 31 chars | 147 |
| list_files_in_folder | run | PASS | reply 865 chars | 6 |
| list_open_windows | run | PASS | reply 57 chars | 972 |
| list_reminders | run | PASS | reply 27 chars | 87 |
| list_running_applications | run | PASS | reply 91 chars | 47 |
| now_playing | run | PASS | reply 50 chars | 36 |
| random_pick | run | PASS | reply 11 chars | 3 |
| read_screen_text | run | PASS | reply 164 chars | 200 |
| read_webpage | run | PASS | reply 206 chars | 404 |
| recent_calls | run | KNOWN | needs Full Disk Access (System Settings) — the tool explains this | 8 |
| recent_files | run | PASS | reply 53 chars | 877 |
| retrieve_memories | run | PASS | reply 38 chars | 447 |
| scrape_webpage | run | PASS | reply 35 chars | 1381 |
| screen_find | run | PASS | reply 20 chars | 256 |
| screen_observe | run | PASS | reply 1490 chars | 131 |
| search_web | run | PASS | reply 4926 chars | 5022 |
| smart_search | run | PASS | reply 4943 chars | 2515 |
| system_load | run | PASS | reply 108 chars | 354 |
| uptime | run | PASS | reply 27 chars | 14 |
| vision_status | run | PASS | reply 68 chars | 3 |
| web_knowledge | run | PASS | reply 42 chars | 8 |
| web_search | run | PASS | reply 4926 chars | 10884 |
| wifi_status | run | PASS | reply 26 chars | 218 |
| world_time | run | PASS | reply 54 chars | 25 |
| create_folder | sandbox | PASS | ok | 12 |
| create_file | sandbox | PASS | ok | 11 |
| read_file_text | sandbox | PASS | ok | 12 |
| copy_file | sandbox | PASS | ok | 7 |
| rename_file | sandbox | PASS | ok | 3 |
| move_file | sandbox | PASS | ok | 10 |
| zip_files | sandbox | PASS | ok | 11 |
| file_info | sandbox | PASS | ok | 4 |
| (file chain result) | sandbox | PASS | copy → rename → move → zip produced the expected files | 0 |
| set_volume | restore | PASS | 100→99→100 | 142 |
| mute | restore | PASS | toggled and put back | 197 |
| set_clipboard | restore | PASS | set, read back, restored | 47 |
| keep_awake | restore | PASS | ok (harmless, ends on its own) | 11 |
| show_notification | restore | PASS | ok (harmless, ends on its own) | 169 |
| delete_file | gate | PASS | asks: Move note.txt to the Trash? | 5 |
| empty_trash | gate | PASS | asks: Empty the Trash? Everything in it is deleted for good. | 2 |
| call_contact | gate | PASS | asks: Call Test Number on 5550100? | 2 |
| deliver_chat_message | gate | PASS | asks: Send 'test' to Test on WhatsApp? | 2 |
| type_and_submit | gate | PASS | asks: Type “test” in Brave Browser and press Enter? | 13 |
| add_event | gate | PASS | asks: Add “DEMASCAS tool check” to your calendar for tomorrow 9am? | 2 |
| open_application | live | PASS | on Calculator: Success: Opened and activated 'Calculator'. | 4428 |
| activate_application | live | PASS | on Calculator: Success: Activated 'Calculator'. | 331 |
| wait_for_app_ready | live | PASS | on Calculator: Success: 'Calculator' is running with windows open. | 236 |
| window_action | live | PASS | on Calculator: Moved Calculator — its window has a fixed size, so I couldn' | 108 |
| window_action | live | PASS | on Calculator: Minimized Calculator. | 38 |
| hide_app | live | PASS | on Calculator: Hid Calculator. | 67 |
| quit_application | live | PASS | on Calculator: Success: Quit 'Calculator'. | 120 |
| open_file | live | PASS | scratch file | 106 |
| reveal_in_finder | live | PASS | scratch file | 88 |
| browser_get_url | live | PASS | read-only, current tab | 182 |
| browser_get_title | live | PASS | read-only, current tab | 109 |
| browser_list_tabs | live | PASS | read-only, current tab | 429 |
| browser_get_page_text | live | PASS | read-only, current tab | 163 |
| browser_get_dom_summary | live | PASS | read-only, current tab | 110 |
| add_note | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| architect_plan | slow | SKIP | needs --slow | 0 |
| brightness | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| browser_back | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_check_checkbox | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_click | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_click_text | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_click_xpath | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_close_tab | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_element_exists | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_execute_js | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_fill_form | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_forward | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_get_inputs | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_get_links | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_get_page_html | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_navigate | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_navigate_and_wait | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_new_tab | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_press_key | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_reload | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_scroll | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_scroll_to_element | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_select_option | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_submit_form | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_switch_to_tab | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_type | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_type_by_label | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_wait_for_element | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| browser_wait_for_page_load | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| click_menu_item | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| click_ui_element | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| compose_email | slow | SKIP | needs --slow | 0 |
| confirm_and_execute | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| create_document | slow | SKIP | needs --slow | 0 |
| create_presentation | slow | SKIP | needs --slow | 0 |
| create_spreadsheet | slow | SKIP | needs --slow | 0 |
| dark_mode | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| describe_screen | slow | SKIP | needs --slow | 0 |
| draft_mail | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| drag_element | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| edit_presentation | slow | SKIP | needs --slow | 0 |
| extract_contact | slow | SKIP | needs --slow | 0 |
| find_and_click | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| forget_web | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| gmail_compose_draft | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| gmail_read_email | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| guide_app | slow | SKIP | needs --slow | 0 |
| learn_app | slow | SKIP | needs --slow | 0 |
| lock_screen | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| look_at_image | slow | SKIP | needs --slow | 0 |
| media_control | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| message_contact | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| morning_brief | slow | SKIP | needs --slow | 0 |
| open_chat | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| open_copied_path | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| open_settings_pane | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| open_url | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| open_url_and_wait | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| press_keyboard_shortcut | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| read_chat_messages | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| read_current_email | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| read_mail | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| read_notes | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| remind_me | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| remove_preference | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| research_web | slow | SKIP | needs --slow | 0 |
| right_click_element | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| run_app_script | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| run_shell_command | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| save_memory | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| save_user_preference | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| scroll_direction | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| scroll_to_element | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| send_chat_message | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| set_default_browser | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| set_humor_level | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| set_preference | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| set_wifi | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| sleep_display | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| smart_reply | slow | SKIP | needs --slow | 0 |
| summarize_latest_pdf | slow | SKIP | needs --slow | 0 |
| summarize_selected_pdf | slow | SKIP | needs --slow | 0 |
| switch_tab | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| take_screenshot | live | SKIP | never run by the check (changes the system or reaches people) | 0 |
| type_text | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| type_text_into_element | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| ui_click | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| ui_scroll | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| ui_select | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| ui_set_checked | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| ui_type | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| ui_undo | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| unwatch_topic | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| vision_execute | slow | SKIP | needs --slow | 0 |
| wait_for_element | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| watch_topic | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| web_analyze_page | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| web_ecommerce_search | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| web_execute_goal | slow | SKIP | needs --slow | 0 |
| web_handle_obstacle | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| web_read_article | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| web_research | slow | SKIP | needs --slow | 0 |
| web_research_and_report | slow | SKIP | needs --slow | 0 |
| web_scan_for_obstacles | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| web_smart_click | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| web_youtube_play_first | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
| web_youtube_search | live | SKIP | wiring OK; opens/clicks/types — run with --live | 0 |
