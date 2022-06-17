#! /usr/bin/python
import os
import sys
import pickle
import datetime

import numpy as np

# Import keras + tensorflow without the "Using XX Backend" message
stderr = sys.stderr
sys.stderr = open(os.devnull, 'w')
import tensorflow as tf
from keras.models import Sequential, Model
from keras.layers import Input, Activation, Add
from keras.layers import BatchNormalization, LeakyReLU, PReLU, Conv2D, Dense
from keras.layers import UpSampling2D, Lambda
from keras.optimizers import Adam
from keras.applications import VGG19
from keras.applications.vgg19 import preprocess_input
from keras.utils.data_utils import OrderedEnqueuer
from keras import backend as K
from keras.callbacks import TensorBoard, ModelCheckpoint, LambdaCallback
sys.stderr = stderr

from libs.util import DataLoader, plot_test_images


class SRGAN():
    """
    Implementation of SRGAN as described in the paper:
    Photo-Realistic Single Image Super-Resolution Using a Generative Adversarial Network
    https://arxiv.org/abs/1609.04802
    """

    def __init__(self, 
        height_lr=24, width_lr=24, channels=3,
        upscaling_factor=4, 
        gen_lr=1e-4, dis_lr=1e-4, 
        # VGG scaled with 1/12.75 as in paper
        loss_weights=[1e-3, 0.006],
        training_mode=True
    ):
        """        
        :param int height_lr: Height of low-resolution images
        :param int width_lr: Width of low-resolution images
        :param int channels: Image channels
        :param int upscaling_factor: Up-scaling factor
        :param int gen_lr: Learning rate of generator
        :param int dis_lr: Learning rate of discriminator
        """
        
        # Low-resolution image dimensions
        self.height_lr = height_lr
        self.width_lr = width_lr

        # High-resolution image dimensions
        if upscaling_factor not in [2, 4, 8]:
            raise ValueError('Upscaling factor must be either 2, 4, or 8. You chose {}'.format(upscaling_factor))
        self.upscaling_factor = upscaling_factor
        self.height_hr = int(self.height_lr * self.upscaling_factor)
        self.width_hr = int(self.width_lr * self.upscaling_factor)

        # Low-resolution and high-resolution shapes
        self.channels = channels
        self.shape_lr = (self.height_lr, self.width_lr, self.channels)
        self.shape_hr = (self.height_hr, self.width_hr, self.channels)

        # Learning rates
        self.gen_lr = gen_lr
        self.dis_lr = dis_lr
        
        # Scaling of losses
        self.loss_weights = loss_weights

        # Gan setup settings
        self.gan_loss = 'mse'
        self.dis_loss = 'binary_crossentropy'
        
        # Build & compile the generator network
        self.generator = self.build_generator()
        self.compile_generator(self.generator)

        # If training, build rest of GAN network
        if training_mode:
            self.vgg = self.build_vgg()
            self.compile_vgg(self.vgg)
            self.discriminator = self.build_discriminator()
            self.compile_discriminator(self.discriminator)
            self.srgan = self.build_srgan()
            self.compile_srgan(self.srgan)

    def save_weights(self, filepath, e=None):
        """Save the generator and discriminator networks"""
        self.generator.save_weights("{}_generator_{}X_epoch{}.h5".format(filepath, self.upscaling_factor, e))
        self.discriminator.save_weights("{}_discriminator_{}X_epoch{}.h5".format(filepath, self.upscaling_factor, e))

    def load_weights(self, generator_weights=None, discriminator_weights=None, **kwargs):
        if generator_weights:
            self.generator.load_weights(generator_weights, **kwargs)
        if discriminator_weights:
            self.discriminator.load_weights(discriminator_weights, **kwargs)
            
    def SubpixelConv2D(self, name, scale=2):
        """
        Keras layer to do subpixel convolution.
        NOTE: Tensorflow backend only. Uses tf.depth_to_space
        
        :param scale: upsampling scale compared to input_shape. Default=2
        :return:
        """

        def subpixel_shape(input_shape):
            dims = [input_shape[0],
                    None if input_shape[1] is None else input_shape[1] * scale,
                    None if input_shape[2] is None else input_shape[2] * scale,
                    int(input_shape[3] / (scale ** 2))]
            output_shape = tuple(dims)
            return output_shape

        def subpixel(x):
            return tf.depth_to_space(x, scale)

        return Lambda(subpixel, output_shape=subpixel_shape, name=name)

    def build_vgg(self):
        """
        Load pre-trained VGG weights from keras applications
        Extract features to be used in loss function from last conv layer, see architecture at:
        https://github.com/keras-team/keras/blob/master/keras/applications/vgg19.py
        """

        # Input image to extract features from
        img = Input(shape=self.shape_hr)

        # Get the vgg network. Extract features from last conv layer
        vgg = VGG19(weights="imagenet")
        vgg.outputs = [vgg.layers[20].output]

        # Create model and compile
        model = Model(inputs=img, outputs=vgg(img))
        model.trainable = False
        return model    
    
    def preprocess_vgg(self, x):
        """Take a HR image [-1, 1], convert to [0, 255], then to input for VGG network"""
        if isinstance(x, np.ndarray):
            return preprocess_input((x+1)*127.5)
        else:            
            return Lambda(lambda x: preprocess_input(tf.add(x, 1) * 127.5))(x)     

    def build_generator(self, residual_blocks=16):
        """
        Build the generator network according to description in the paper.

        :param optimizer: Keras optimizer to use for network
        :param int residual_blocks: How many residual blocks to use
        :return: the compiled model
        """

        def residual_block(input):
            x = Conv2D(64, kernel_size=3, strides=1, padding='same')(input)
            x = BatchNormalization(momentum=0.8)(x)
            x = PReLU(shared_axes=[1,2])(x)            
            x = Conv2D(64, kernel_size=3, strides=1, padding='same')(x)
            x = BatchNormalization(momentum=0.8)(x)
            x = Add()([x, input])
            return x

        def upsample(x, number):
            x = Conv2D(256, kernel_size=3, strides=1, padding='same', name='upSampleConv2d_'+str(number))(x)
            x = self.SubpixelConv2D('upSampleSubPixel_'+str(number), 2)(x)
            x = PReLU(shared_axes=[1,2], name='upSamplePReLU_'+str(number))(x)
            return x

        # Input low resolution image
        lr_input = Input(shape=(None, None, 3))

        # Pre-residual
        x_start = Conv2D(64, kernel_size=9, strides=1, padding='same')(lr_input)
        x_start = PReLU(shared_axes=[1,2])(x_start)

        # Residual blocks
        r = residual_block(x_start)
        for _ in range(residual_blocks - 1):
            r = residual_block(r)

        # Post-residual block
        x = Conv2D(64, kernel_size=3, strides=1, padding='same')(r)
        x = BatchNormalization(momentum=0.8)(x)
        x = Add()([x, x_start])
        
        # Upsampling depending on factor
        x = upsample(x, 1)
        if self.upscaling_factor > 2:
            x = upsample(x, 2)
        if self.upscaling_factor > 4:
            x = upsample(x, 3)
        
        # Generate high resolution output
        # tanh activation, see: 
        # https://towardsdatascience.com/gan-ways-to-improve-gan-performance-acf37f9f59b
        hr_output = Conv2D(
            self.channels, 
            kernel_size=9, 
            strides=1, 
            padding='same', 
            activation='tanh'
        )(x)

        # Create model and compile
        model = Model(inputs=lr_input, outputs=hr_output)        
        return model    

    def build_discriminator(self, filters=64):
        """
        Build the discriminator network according to description in the paper.

        :param optimizer: Keras optimizer to use for network
        :param int filters: How many filters to use in first conv layer
        :return: the compiled model
        """

        def conv2d_block(input, filters, strides=1, bn=True):
            d = Conv2D(filters, kernel_size=3, strides=strides, padding='same')(input)
            d = LeakyReLU(alpha=0.2)(d)
            if bn:
                d = BatchNormalization(momentum=0.8)(d)
            return d

        # Input high resolution image
        img = Input(shape=self.shape_hr)
        x = conv2d_block(img, filters, bn=False)
        x = conv2d_block(x, filters, strides=2)
        x = conv2d_block(x, filters*2)
        x = conv2d_block(x, filters*2, strides=2)
        x = conv2d_block(x, filters*4)
        x = conv2d_block(x, filters*4, strides=2)
        x = conv2d_block(x, filters*8)
        x = conv2d_block(x, filters*8, strides=2)
        x = Dense(filters*16)(x)
        x = LeakyReLU(alpha=0.2)(x)
        x = Dense(1, activation='sigmoid')(x)

        # Create model and compile
        model = Model(inputs=img, outputs=x)
        return model

    def build_srgan(self):
        """Create the combined SRGAN network"""

        # Input LR images
        img_lr = Input(self.shape_lr)

        # Create a high resolution image from the low resolution one
        generated_hr = self.generator(img_lr)
        generated_features = self.vgg(
            self.preprocess_vgg(generated_hr)
        )

        # In the combined model we only train the generator
        self.discriminator.trainable = False

        # Determine whether the generator HR images are OK
        generated_check = self.discriminator(generated_hr)
        
        # Create sensible names for outputs in logs
        generated_features = Lambda(lambda x: x, name='Content')(generated_features)
        generated_check = Lambda(lambda x: x, name='Adversarial')(generated_check)

        # Create model and compile
        # Using binary_crossentropy with reversed label, to get proper loss, see:
        # https://danieltakeshi.github.io/2017/03/05/understanding-generative-adversarial-networks/
        model = Model(inputs=img_lr, outputs=[generated_check, generated_features])        
        return model

    def compile_vgg(self, model):
        """Compile the generator with appropriate optimizer"""
        model.compile(
            loss='mse',
            optimizer=Adam(self.gen_lr, 0.9),
            metrics=['accuracy']
        )

    def compile_generator(self, model):
        """Compile the generator with appropriate optimizer"""
        model.compile(
            loss=self.gan_loss,
            optimizer=Adam(self.gen_lr, 0.9),
            metrics=['mse', self.PSNR]
        )

    def compile_discriminator(self, model):
        """Compile the generator with appropriate optimizer"""
        model.compile(
            loss=self.dis_loss,
            optimizer=Adam(self.dis_lr, 0.9),
            metrics=['accuracy']
        )

    def compile_srgan(self, model):
        """Compile the GAN with appropriate optimizer"""
        model.compile(
            loss=[self.dis_loss, self.gan_loss],
            loss_weights=self.loss_weights,
            optimizer=Adam(self.gen_lr, 0.9)
        )
    
    def PSNR(self, y_true, y_pred):
        """
        PSNR is Peek Signal to Noise Ratio, see https://en.wikipedia.org/wiki/Peak_signal-to-noise_ratio

        The equation is:
        PSNR = 20 * log10(MAX_I) - 10 * log10(MSE)
        
        Since input is scaled from -1 to 1, MAX_I = 1, and thus 20 * log10(1) = 0. Only the last part of the equation is therefore neccesary.
        """
        return -10.0 * K.log(K.mean(K.square(y_pred - y_true))) / K.log(10.0) 
    
    def train_generator(self,
        epochs, batch_size,
        workers=1,
        dataname='train_gen',
        datapath_train='./train_dir',
        datapath_validation='./val_dir',
        datapath_test='./val_dir',
        steps_per_epoch=1000,
        steps_per_validation=1000,
        crops_per_image=4,
        log_weight_path='./data/weights/', 
        log_tensorboard_path='./data/logs/',
        log_tensorboard_name='SRResNet',
        log_tensorboard_update_freq=1,
        log_test_path="./images/samples/"
    ):
        """Trains the generator part of the network with MSE loss"""        

        # Create data loaders
        train_loader = DataLoader(
            datapath_train, batch_size,
            self.height_hr, self.width_hr,
            self.upscaling_factor,
            crops_per_image
        )
        test_loader = None
        if datapath_validation is not None:
            test_loader = DataLoader(
                datapath_validation, batch_size,
                self.height_hr, self.width_hr,
                self.upscaling_factor,
                crops_per_image
            )
        
        # Callback: tensorboard
        callbacks = []
        if log_tensorboard_path:
            tensorboard = TensorBoard(
                log_dir=os.path.join(log_tensorboard_path, log_tensorboard_name),
                histogram_freq=0,
                batch_size=batch_size,
                write_graph=False,
                write_grads=False,
                update_freq=log_tensorboard_update_freq
            )
            callbacks.append(tensorboard)
        else:
            print(">> Not logging to tensorboard since no log_tensorboard_path is set")
        
        # Callback: save weights after each epoch
        modelcheckpoint = ModelCheckpoint(
            os.path.join(log_weight_path, dataname + '_{}X'.format(self.upscaling_factor)), 
            monitor='val_loss', 
            save_best_only=True, 
            save_weights_only=True
        )
        callbacks.append(modelcheckpoint)
        
        # Callback: test images plotting
        if datapath_test is not None:
            testplotting = LambdaCallback(
                on_epoch_end=lambda epoch, logs: plot_test_images(
                    self, 
                    test_loader,
                    datapath_test, 
                    log_test_path, 
                    epoch, 
                    name='SRResNet'
                )
            )
            callbacks.append(testplotting)
                            
        # Fit the model
        self.generator.fit_generator(
            train_loader,
            steps_per_epoch=steps_per_epoch,
            epochs=epochs,
            validation_data=test_loader,
            validation_steps=steps_per_validation,
            callbacks=callbacks,
            use_multiprocessing=workers>1,
            workers=workers
        )

    def train_srgan(self, 
        epochs, batch_size, 
        dataname, 
        datapath_train,
        datapath_validation=None, 
        steps_per_validation=10,
        datapath_test=None, 
        workers=40, max_queue_size=100,
        first_epoch=0,
        print_frequency=50,
        crops_per_image=1,
        log_weight_frequency=500,
        log_weight_path='./data/weights/', 
        log_tensorboard_path='./data/logs/',
        log_tensorboard_name='SRGAN',
        log_tensorboard_update_freq=500,
        log_test_frequency=500,
        log_test_path="./images/samples/",         
    ):
        """Train the SRGAN network

        :param int epochs: how many epochs to train the network for
        :param str dataname: name to use for storing model weights etc.
        :param str datapath_train: path for the image files to use for training
        :param str datapath_test: path for the image files to use for testing / plotting
        :param int print_frequency: how often (in epochs) to print progress to terminal. Warning: will run validation inference!
        :param int log_weight_frequency: how often (in epochs) should network weights be saved. None for never
        :param int log_weight_path: where should network weights be saved        
        :param int log_test_frequency: how often (in epochs) should testing & validation be performed
        :param str log_test_path: where should test results be saved
        :param str log_tensorboard_path: where should tensorflow logs be sent
        :param str log_tensorboard_name: what folder should tf logs be saved under        
        """

        # Create train data loader
        loader = DataLoader(
            datapath_train, batch_size,
            self.height_hr, self.width_hr,
            self.upscaling_factor,
            crops_per_image
        )

        # Validation data loader
        if datapath_validation is not None:
            validation_loader = DataLoader(
                datapath_validation, batch_size,
                self.height_hr, self.width_hr,
                self.upscaling_factor,
                crops_per_image
            )
        print("Picture Loaders has been ready.")
        # Use several workers on CPU for preparing batches
        enqueuer = OrderedEnqueuer(
            loader,
            use_multiprocessing=False,
            shuffle=True
        )
        enqueuer.start(workers=workers, max_queue_size=max_queue_size)
        output_generator = enqueuer.get()
        print("Data Enqueuer has been ready.")
        # Callback: tensorboard
        if log_tensorboard_path:
            tensorboard = TensorBoard(
                log_dir=os.path.join(log_tensorboard_path, log_tensorboard_name),
                histogram_freq=0,
                batch_size=batch_size,
                write_graph=False,
                write_grads=False,
                update_freq=log_tensorboard_update_freq
            )
            tensorboard.set_model(self.srgan)
        else:
            print(">> Not logging to tensorboard since no log_tensorboard_path is set")
        
        # Callback: format input value
        def named_logs(model, logs):
            """Transform train_on_batch return value to dict expected by on_batch_end callback"""
            result = {}
            for l in zip(model.metrics_names, logs):
                result[l[0]] = l[1]
            return result

        # Shape of output from discriminator
        disciminator_output_shape = list(self.discriminator.output_shape)
        disciminator_output_shape[0] = batch_size
        disciminator_output_shape = tuple(disciminator_output_shape)

        # VALID / FAKE targets for discriminator
        real = np.ones(disciminator_output_shape)
        fake = np.zeros(disciminator_output_shape)        

        # Each epoch == "update iteration" as defined in the paper        
        print_losses = {"G": [], "D": []}
        start_epoch = datetime.datetime.now()
        
        # Random images to go through
        idxs = np.random.randint(0, len(loader), epochs)        
        
        # Loop through epochs / iterations
        for epoch in range(first_epoch, int(epochs)+first_epoch):
            print(epoch)
            # Start epoch time
            if epoch % print_frequency == 1:
                start_epoch = datetime.datetime.now()            

            # Train discriminator   
            imgs_lr, imgs_hr = next(output_generator)
            generated_hr = self.generator.predict(imgs_lr)
            real_loss = self.discriminator.train_on_batch(imgs_hr, real)
            fake_loss = self.discriminator.train_on_batch(generated_hr, fake)
            discriminator_loss = 0.5 * np.add(real_loss, fake_loss)

            # Train generator
            features_hr = self.vgg.predict(self.preprocess_vgg(imgs_hr))
            generator_loss = self.srgan.train_on_batch(imgs_lr, [real, features_hr])            

            # Callbacks
            logs = named_logs(self.srgan, generator_loss)
            tensorboard.on_epoch_end(epoch, logs)
            # print(generator_loss, discriminator_loss)
            # Save losses            
            print_losses['G'].append(generator_loss)
            print_losses['D'].append(discriminator_loss)

            # Show the progress
            if epoch % print_frequency == 0:
                g_avg_loss = np.array(print_losses['G']).mean(axis=0)
                d_avg_loss = np.array(print_losses['D']).mean(axis=0)
                print("\nEpoch {}/{} | Time: {}s\n>> Generator/GAN: {}\n>> Discriminator: {}".format(
                    epoch, epochs+first_epoch,
                    (datetime.datetime.now() - start_epoch).seconds,
                    ", ".join(["{}={:.4f}".format(k, v) for k, v in zip(self.srgan.metrics_names, g_avg_loss)]),
                    ", ".join(["{}={:.4f}".format(k, v) for k, v in zip(self.discriminator.metrics_names, d_avg_loss)])
                ))
                print_losses = {"G": [], "D": []}
                # Run validation inference if specified
                # if datapath_validation:
                #     print(">> Running validation inference")
                #     validation_losses = self.generator.evaluate_generator(
                #         validation_loader,
                #         steps=steps_per_validation,
                #         use_multiprocessing=workers>1,
                #         workers=workers
                #     )
                #     print(">> Validation Losses: {}".format(
                #         ", ".join(["{}={:.4f}".format(k, v) for k, v in zip(self.generator.metrics_names, validation_losses)])
                #     ))

            # If test images are supplied, run model on them and save to log_test_path
            if datapath_test and epoch % log_test_frequency == 0:
                print(">> Ploting test images")
                plot_test_images(self, loader, datapath_test, log_test_path, epoch)

            # Check if we should save the network weights
            if log_weight_frequency and epoch % log_weight_frequency == 0:
                # Save the network weights
                print(">> Saving the network weights")
                self.save_weights(os.path.join(log_weight_path, dataname), epoch)

    def test(self,
        refer_model=None,
        batch_size=4,
        datapath_test='./images/val_dir',
        crops_per_image=1,
        log_test_path="./images/test/"
    ):
        """Trains the generator part of the network with MSE loss"""

        # Create data loaders
        loader = DataLoader(
            datapath_test, batch_size,
            self.height_hr, self.width_hr,
            self.upscaling_factor,
            crops_per_image
        )
        print(">> Ploting test images")
        plot_test_images(self, loader, datapath_test, log_test_path, 0, refer_model=refer_model)


# Run the SRGAN network
if __name__ == '__main__':

    # Instantiate the SRGAN object
    print(">> Creating the SRGAN network")
    gan = SRGAN(gen_lr=1e-5, dis_lr=1e-5)
    # gan.generator.load_weights('./data/weights/imagenet_generator.h5')
    # gan.train_generator(
    #     epochs=150,
    #     batch_size=64,
    # )
    # gan.save('./data/weights/imagenet_gan.h5')

    # Load previous imagenet weights
    print(">> Loading old weights")
    gan.load_weights('./data/weights/_generator_4X_epochNone.h5', './data/weights/_discriminator_4X_epochNone.h5')
    # gan.generator.load_weights('./data/weights/DIV2K_generator.h5')

    # Train the SRGAN
    # gan.train_srgan(
    #     epochs=20000,
    #     first_epoch=60000,
    #     batch_size=64,
    #     dataname='DIV2K',
    #     datapath_train='./images/224x224_train/',
    #     # datapath_validation='./images/DIV2K_valid_HR/',
    #     datapath_test='./images/val_dir/',
    #     print_frequency=20,
    # )
    # gan.save_weights('./data/weights/')

