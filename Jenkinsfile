pipeline {
  agent any

  parameters {
    booleanParam(defaultValue: false, description: 'Execute in dry run mode', name: 'DRY_RUN')
  }
  environment {
    ANSIBLE_SERVER = "<your_ansible_server>"
    REMOTE_DIR = "/etc/ansible/playbooks/ebs-autoscaler"
    TEMP_DIR = "~/tmp/ebs-autoscaler"
    EC2_HOST = "<your_ansible_user>@${env.ANSIBLE_SERVER}"
  }

  stages {
    stage('Copy Ansible Files and Resources') {
      steps {
        sh '''
            cd aws/ebs/ebs-autoscaler
            echo "Copying Ansible files and resources to EC2 instance..."
            ssh -o StrictHostKeyChecking=no $EC2_HOST "mkdir -p $TEMP_DIR $TEMP_DIR/resources $TEMP_DIR/inventory $TEMP_DIR/inventory/group_vars"
            scp -o StrictHostKeyChecking=no -r ansible/* playbook.yml $EC2_HOST:$TEMP_DIR
            scp -o StrictHostKeyChecking=no -r config.ini ebs-scaler.py ebs-scaler.service.j2 $EC2_HOST:$TEMP_DIR/resources
            ssh -o StrictHostKeyChecking=no $EC2_HOST "if [ -d \"$REMOTE_DIR\" ]; then sudo rm -rf $REMOTE_DIR; fi && sudo mkdir -p $REMOTE_DIR && sudo mv $TEMP_DIR/* $REMOTE_DIR"
        '''
      }
    }
    stage('Dry Run Ansible Playbook') {
        when {
            expression {
                return params.DRY_RUN == true
            }
        }
        steps {
            sh '''
            ssh -o StrictHostKeyChecking=no $EC2_HOST "cd $REMOTE_DIR && LC_ALL=C.UTF-8 ansible-inventory -i inventory/ --graph"
            ssh -o StrictHostKeyChecking=no $EC2_HOST "cd $REMOTE_DIR && LC_ALL=C.UTF-8 ansible-playbook playbook.yml -i inventory/ --check"
            '''
        }
    }
    stage('Run Ansible Playbook') {
      when {
        expression {
            return params.DRY_RUN == false
        }
      }
      steps {
        sh '''
          ssh -o StrictHostKeyChecking=no $EC2_HOST "cd $REMOTE_DIR && LC_ALL=C.UTF-8 ansible-inventory -i inventory/ --graph"
          ssh -o StrictHostKeyChecking=no $EC2_HOST "cd $REMOTE_DIR && LC_ALL=C.UTF-8 ansible-playbook playbook.yml -i inventory/ --check"
        '''
      }
    }
  }
}
