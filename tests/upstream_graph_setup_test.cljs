(ns logseq.db-sync.graph-setup-test
  (:require [logseq.db-sync.worker.allowlist :as allowlist]
            [promesa.core :as p]))

(defn main []
  (let [graph-id "33333333-3333-4333-8333-333333333333"
        secret (apply str (repeat 64 "s"))
        tokens (atom [])
        db #js {:prepare
                (fn [sql]
                  (let [params (atom [])
                        statement #js {}]
                    (aset statement "bind" (fn [& args] (reset! params args) statement))
                    (aset statement "all"
                          (fn []
                            (assert (.includes sql "u.email_verified = 1"))
                            (assert (.includes sql "g.graph_ready_for_use = 1"))
                            (assert (.includes sql "training_allowed_accounts"))
                            (assert (.includes sql "m.user_id = u.id"))
                            (assert (= graph-id (second @params)))
                            (p/resolved #js {:results (if (= "owner@example.com" (first @params))
                                                       #js [#js {:user_id "owner-id" :graph_e2ee 1
                                                                 :encrypted_private_key "private-package"
                                                                 :encrypted_aes_key "aes-package"}]
                                                       #js [])})))
                    (aset statement "run"
                          (fn []
                            (assert (.startsWith sql "insert into personal_access_tokens"))
                            (swap! tokens conj @params)
                            (p/resolved #js {:success true})))
                    statement))}
        env #js {:DB db :TRAINING_OWNER_EMAIL "owner@example.com" :TRAINING_ALLOWLIST_TOKEN secret}
        request (fn [method email token]
                  (js/Request. (str "http://internal/training/graph-access?email=" email "&graph_id=" graph-id)
                               #js {:method method :headers #js {:authorization (str "Bearer " token)}}))]
    (p/let [unauth (allowlist/handle (request "GET" "owner@example.com" "wrong") env)
            denied (allowlist/handle (request "GET" "outsider@example.com" secret) env)
            denied-post (allowlist/handle (request "POST" "outsider@example.com" secret) env)
            wrong-method (allowlist/handle (request "DELETE" "owner@example.com" secret) env)
            bundle-response (allowlist/handle (request "GET" "owner@example.com" secret) env)
            bundle (.json bundle-response)]
      (assert (= 401 (.-status unauth)))
      (assert (= 403 (.-status denied)))
      (assert (= 403 (.-status denied-post)))
      (assert (= 405 (.-status wrong-method)))
      (assert (= 200 (.-status bundle-response)))
      (assert (true? (aget bundle "encrypted")))
      (assert (= "private-package" (aget bundle "encrypted_private_key")))
      (assert (empty? @tokens))
      (p/let [response (allowlist/handle (request "POST" "owner@example.com" secret) env)
              body (.json response)
              token (aget body "token")
              digest (.digest (.-subtle js/crypto) "SHA-256" (.encode (js/TextEncoder.) token))]
        (assert (= 200 (.-status response)))
        (assert (re-matches #"logseq_pat_[0-9a-f]{64}" token))
        (assert (= graph-id (aget body "graph_id")))
        (assert (= 1 (count @tokens)))
        (let [[_ user-id scoped-graph hash prefix permission created expires] (first @tokens)]
          (assert (= "owner-id" user-id))
          (assert (= graph-id scoped-graph))
          (assert (= "both" permission))
          (assert (= (subs token 0 23) prefix))
          (assert (= hash (apply str (map #(.padStart (.toString % 16) 2 "0") (array-seq (js/Uint8Array. digest))))))
          (assert (= expires (aget body "expires_at")))
          (assert (= (* 365 24 60 60 1000) (- expires created)))))
      (println "PASS graph setup: management auth, membership checks, encrypted packages, scoped hashed tokens"))))
