// Regenerates market.json from the official upstream market + the loaders' APIs
// and pushes it to main when (and only when) the content changed.
// Runs on the controller node (label docker-host), which has python3 and git.
pipeline {
    agent { label 'docker-host' }

    options {
        disableConcurrentBuilds()
        timeout(time: 15, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '40'))
    }

    triggers {
        cron('H H/6 * * *')
    }

    environment {
        PUSH_URL = 'git@github.com:aaron777collins/mcsmanager-market.git'
    }

    stages {
        stage('Generate') {
            steps {
                sh 'python3 --version'
                sh 'python3 generate.py'
            }
        }

        stage('Publish') {
            steps {
                withCredentials([sshUserPrivateKey(credentialsId: 'mcsmanager-market-deploy-key', keyFileVariable: 'DEPLOY_KEY')]) {
                    sh '''
                        set -eu
                        if git diff --quiet -- market.json; then
                            echo "market.json unchanged, nothing to publish"
                            exit 0
                        fi
                        git diff -U0 -- market.json | sed -n 's/^+ *"title": "\\(.*\\)",\\?$/\\1/p' | sort -u > .added
                        git diff -U0 -- market.json | sed -n 's/^- *"title": "\\(.*\\)",\\?$/\\1/p' | sort -u > .removed
                        newonly=$(comm -23 .added .removed | head -12 | paste -sd, - | sed 's/,/, /g')
                        msg="Update market.json"
                        [ -n "$newonly" ] && msg="$msg: $newonly"
                        git add market.json
                        git -c user.name="mcsmanager-market bot" \
                            -c user.email="mcsmanager-market-bot@users.noreply.github.com" \
                            commit -m "$msg"
                        export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$WORKSPACE/.known_hosts"
                        git push "$PUSH_URL" HEAD:main
                    '''
                }
            }
        }
    }
}
