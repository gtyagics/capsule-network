import os
import argparse
import numpy as np
from PIL import Image
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D

from keras import callbacks, layers, models, optimizers
from keras import backend as K
from keras.utils import to_categorical
from keras.datasets import mnist
from keras.preprocessing.image import ImageDataGenerator

from sklearn.metrics import confusion_matrix, f1_score, accuracy_score, recall_score, precision_score

import utils
from capsule import PrimaryCaps, CapsuleLayer, Length, Mask, margin_loss, reconstruction_loss
import symmetric_dataset


#
# Set defaults
#
K.set_image_data_format('channels_last')
capsnet_out_dim = 3
WIDTH = 28
HEIGHT = 28

#
# Main
#
def main(args):
    # Ensure working dirs
    if not os.path.exists(args.save_dir):
            os.makedirs(args.save_dir)

    # Save args into file 
    if not args.testing:
        with open(args.save_dir+"/args.txt", "w") as out:
            sorted_args = sorted(vars(args).items())
            out.write('\n'.join("{0} = {1}".format(a, v) for (a, v) in sorted_args))

        
    # Load data
    (x_train, y_train), (x_test, y_test) = load_dataset()

    # Cut off training samples
    if(args.max_num_samples is not None):
        x_train = x_train[:args.max_num_samples]
        y_train = y_train[:args.max_num_samples]
        print("\nUsing only %d training samples.\n" % len(x_train))

    # Create model
    n_class = len(np.unique(np.argmax(y_train, 1)))
    model, eval_model, manipulate_model = create_capsnet(input_shape=x_train.shape[1:],
                                                  out_dim=capsnet_out_dim,
                                                  n_class=n_class,
                                                  num_routing=args.num_routing)
    model.summary()

    # Run training / testing
    if args.weights is not None and os.path.exists(args.weights):
        model.load_weights(args.weights)
        print("Successfully loaded weights file %s" % args.weights)
    
    if not args.testing:
        print("\n" + "=" * 40 + " TRAIN " + "=" * 40)
        train(model=model, data=((x_train, y_train), (x_test, y_test)), args=args)
    else:
        print("\n" + "=" * 40 + " TEST =" + "=" * 40)
        if args.weights is None:
            print('(Warning) No weights are provided, using random initialized weights.')

        show_primary_layer_per_position(model=eval_model)
        #show_primary_layer_output_change(model=eval_model, obj=0)
        #primary_layer_compare(model=eval_model)
        #show_digit_layer_output_phi(model=eval_model, obj=1)
        #show_digit_layer_output_pos(model=eval_model, obj=1)
        
        #test(model=eval_model, data=(x_test, y_test), args=args)
        #manipulate_latent(manipulate_model, n_class, capsnet_out_dim, (x_test, y_test), args)
    
    print("=" * 40 + "=======" + "=" * 40)


def load_dataset():
    (x_train, y_train), (x_test, y_test) = symmetric_dataset.load_data(width=WIDTH, height=HEIGHT, debug=False)

    print("Loaded %d training examples." % len(x_train))
    print("Loaded %d test examples." % len(x_test))

    x_train = x_train.reshape(-1, WIDTH, HEIGHT, 3).astype('float32') / 255.
    x_test = x_test.reshape(-1, WIDTH, HEIGHT, 3).astype('float32') / 255.
    y_train = to_categorical(y_train.astype('float32'))
    y_test = to_categorical(y_test.astype('float32'))
    return (x_train, y_train), (x_test, y_test)


def create_capsnet(input_shape, n_class, out_dim, num_routing):
    # Create CapsNet
    x = layers.Input(shape=input_shape)
    conv1 = layers.Conv2D(filters=64, kernel_size=9, strides=1, padding='valid', activation='relu', name='conv1')(x)
    primary_caps = PrimaryCaps(layer_input=conv1, name='primary_caps', dim_capsule=3, channels=2, kernel_size=9, strides=2)
    digit_caps = CapsuleLayer(num_capsule=n_class, dim_vector=out_dim, num_routing=num_routing)(primary_caps)
    out_caps = Length(name='capsnet')(digit_caps)

    # Create decoder
    y = layers.Input(shape=(n_class,))
    masked_by_y = Mask()([digit_caps, y])    # The true label is used to mask the output of capsule layer for training
    masked = Mask()(digit_caps)              # Mask using the capsule with maximal length for prediction

    # Shared Decoder model in training and prediction
    decoder = models.Sequential(name='decoder')
    decoder.add(layers.Dense(512, activation='relu', input_dim=out_dim*n_class))
    decoder.add(layers.Dense(1024, activation='relu'))
    decoder.add(layers.Dense(np.prod(input_shape), activation='sigmoid'))
    decoder.add(layers.Reshape(target_shape=input_shape, name='decoder_output'))

    # Models for training and evaluation (prediction)
    train_model = models.Model([x, y], [out_caps, decoder(masked_by_y)])
    eval_model = models.Model(x, [out_caps, decoder(masked)])

    # manipulate model
    noise = layers.Input(shape=(n_class, out_dim))
    noised_digit_caps = layers.Add()([digit_caps, noise])
    masked_noised_y = Mask()([noised_digit_caps, y])
    manipulate_model = models.Model([x, y, noise], decoder(masked_noised_y))

    return train_model, eval_model, manipulate_model


def train(model, data, args):
    # unpacking the data
    (x_train, y_train), (x_test, y_test) = data

    # callbacks
    log = callbacks.CSVLogger(args.save_dir + '/log.csv')
    tb = callbacks.TensorBoard(log_dir=args.save_dir + '/tensorboard-logs',
                               batch_size=args.batch_size, histogram_freq=int(args.debug))
    checkpoint = callbacks.ModelCheckpoint(args.save_dir + '/weights-{epoch:02d}.hdf5', monitor='val_capsnet_acc',
                                           save_best_only=False, save_weights_only=True, verbose=1)
    lr_decay = callbacks.LearningRateScheduler(schedule=lambda epoch: args.lr * (args.lr_decay ** epoch))

    # compile the model
    model.compile(optimizer=optimizers.Adam(lr=args.lr),
                  loss=[margin_loss, reconstruction_loss],              # We scale down this reconstruction loss by 0.0005 so that
                  loss_weights=[1., args.scale_reconstruction_loss],    # ...it does not dominate the margin loss during training.
                  metrics={'capsnet': 'accuracy'})                      

    # Generator with data augmentation as used in [1]
    def train_generator_with_augmentation(x, y, batch_size, shift_fraction=0.):
        train_datagen = ImageDataGenerator(width_shift_range=shift_fraction,
                                           height_shift_range=shift_fraction)  # shift up to 2 pixel for MNIST
        generator = train_datagen.flow(x, y, batch_size=batch_size)
        while 1:
            x_batch, y_batch = generator.next()
            yield ([x_batch, y_batch], [y_batch, x_batch])

    generator = train_generator_with_augmentation(x_train, y_train, args.batch_size, args.shift_fraction)
    model.fit_generator(generator=generator,
                        steps_per_epoch=int(y_train.shape[0] / args.batch_size),
                        epochs=args.epochs,
                        validation_data=[[x_test, y_test], [y_test, x_test]],   # Note: For the decoder the input is the label and the output the image
                        callbacks=[log, tb, checkpoint, lr_decay])

    model.save_weights(args.save_dir + '/trained_model.hdf5')
    print('Trained model saved to \'%s/trained_model.hdf5\'' % args.save_dir)

    utils.plot_log(args.save_dir + '/log.csv', show=True)

    return model


def test(model, data, args):

    # Create an augmentation function and cache augmented samples
    # to be displayed later
    x_augmented = []
    def test_generator_with_augmentation(x, batch_size, shift_range, rotation_range):
        test_datagen = ImageDataGenerator(width_shift_range=shift_range,
                                          height_shift_range=shift_range,
                                          rotation_range=rotation_range)
        generator = test_datagen.flow(x, batch_size=batch_size, shuffle=False)
        while 1:
            x_batch = generator.next()
            x_augmented.extend(x_batch)
            yield (x_batch)


    # Initialize data
    test_batch_size = 32
    x_true, y_true = data
    generator = test_generator_with_augmentation(x_true, test_batch_size, args.shift_fraction, args.rotation_range)
    y_pred, x_recon = model.predict_generator(generator=generator, steps=len(x_true) // test_batch_size)
    
    # Print different metrics using the top score
    y_true = np.argmax(y_true, 1)
    y_pred = np.argmax(y_pred, 1)

    # Print metrics
    print('Confusion matrix:\n', confusion_matrix(y_true, y_pred))
    print('\nAccuracy: ', accuracy_score(y_true, y_pred))
    print('Recall: ', recall_score(y_true, y_pred, average='weighted'))
    print('Precision: ', precision_score(y_true, y_pred, average='weighted'))
    print('F1-Score: ', f1_score(y_true, y_pred, average='weighted'))

    # Combine images for manual evaluation
    stacked_img = utils.stack_images_two_arrays(x_augmented, x_recon, 10, 10)
    stacked_img = stacked_img.resize((700, 700), Image.ANTIALIAS)
    stacked_img.show()
    stacked_img.save(args.save_dir + "/real_and_recon.png")

    # Display invalid and correct images
    for i in range(len(x_true)):
        if(y_true[i] == y_pred[i]):
            continue
        invalid_prediction = x_augmented[i]*255
        Image.fromarray(invalid_prediction.astype(np.uint8)).save(args.save_dir + "/wrongly_classified_%d.png" % i)


def manipulate_latent(model, n_class, out_dim, data, args):
    x_true, y_true = data

    index = np.argmax(y_true, 1) == args.manipulate
    number = np.random.randint(low=0, high=sum(index) - 1)
    x, y = x_true[index][number], y_true[index][number]
    x, y = np.expand_dims(x, 0), np.expand_dims(y, 0)
    noise = np.zeros([1, n_class, out_dim])
    x_recons = []

    # Change params of vect in 0.05 steps. See also [1]
    for dim in range(out_dim):
        r = -0.25
        while r <= 0.25:
            tmp = np.copy(noise)
            tmp[:,:,dim] = r
            x_recon = model.predict([x, y, tmp])
            x_recons.append(x_recon[0])
            r += 0.05

    img = utils.stack_images(x_recons, out_dim)
    img.show()
    img.save(args.save_dir + "/manipulate-%d.png" % args.manipulate)


def show_digit_layer_output_phi(model, obj=0):
    """ This function can be used to debug vectors of sample data.
        It prints what a layer outputs for an input.
    """
    
    # Display points in 3D plot
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')
    ax.set_xlabel("DIM=1")
    ax.set_ylabel("DIM=2")
    ax.set_zlabel("DIM=3")

    for i in range(-20, 21):
        phi = i / 10
        _, caps_layer_1 = get_output_for_settings(model, (obj, (0.1,0), phi, (0.4, 0.2)))

        xs = [caps_layer_1[:, 0][obj]]
        ys = [caps_layer_1[:, 1][obj]]
        zs = [caps_layer_1[:, 2][obj]]
        ax.scatter(xs, ys, zs, c='r', marker='o')

        #if i % 5 == 0:
        for k in range(len(xs)):
            ax.text(xs[k], ys[k], zs[k], str(phi), color='red')

    plt.show()


def show_digit_layer_output_pos(model, obj=0):
    """ This function can be used to debug vectors of sample data.
        It prints what a layer outputs for an input.
    """
    
    # Display points in 3D plot
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')
    ax.set_xlabel("DIM=1")
    ax.set_ylabel("DIM=2")
    ax.set_zlabel("DIM=3")

    for x in [0.0, 0.3]:
        c = 'r' if x == 0.0 else 'b'

        for i in range(-5, 6):
            y = i / 10
            _, caps_layer_1 = get_output_for_settings(model, (obj, (x, y), 0, (0.4, 0.2)))

            xs = [caps_layer_1[:, 0][obj]]
            ys = [caps_layer_1[:, 1][obj]]
            zs = [caps_layer_1[:, 2][obj]]
            ax.scatter(xs, ys, zs, c=c, marker='o')

            for k in range(len(xs)):
                ax.text(xs[k], ys[k], zs[k], "{0}:{1}".format(x, y), color=c)

    plt.show()


def show_primary_layer_output_change(model, obj=0):
    """ This function can be used to debug vectors of sample data.
        It prints what a layer outputs for an input.
    """
    
    # Display points in 3D plot
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')
    ax.set_xlabel("DIM=1")
    ax.set_ylabel("DIM=2")
    ax.set_zlabel("DIM=3")

    caps_layer_1, _ = get_output_for_settings(model, (obj, (0.0,0.0), 0, (0.4, 0.2)))
    caps_layer_2, _ = get_output_for_settings(model, (obj, (0.0,0.0), 1, (0.4, 0.2)))

    xs1 = caps_layer_1[:, 0]
    ys1 = caps_layer_1[:, 1]
    zs1 = caps_layer_1[:, 2]
    ax.scatter(xs1, ys1, zs1, c='r', marker='x')

    xs2 = caps_layer_2[:, 0]
    ys2 = caps_layer_2[:, 1]
    zs2 = caps_layer_2[:, 2]
    ax.scatter(xs2, ys2, zs2, c='b', marker='^')

    for i in range(len(xs1)):
        ax.plot([xs1[i], xs2[i]], [ys1[i], ys2[i]], zs=[zs1[i], zs2[i]])

    # Plot 0,0,0 lines
    ax.set_xlim3d([-1,1])
    ax.set_ylim3d([-1,1])
    ax.set_zlim3d([-1,1])

    plt.show()


def show_primary_layer_per_position(model, capsule=1, dim=1, obj=0):
    """ This function can be used to debug vectors of sample data.
        It prints what a layer outputs for an input.
    """
    
    # Display points in 3D plot
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')
    ax.set_xlabel("DIM=1")
    ax.set_ylabel("DIM=2")
    ax.set_zlabel("DIM=3")

    caps_layer_1, _ = get_output_for_settings(model, (obj, (0.0,0.0), 0, (0.3, 0.2)), True)
    caps_layer_2, _ = get_output_for_settings(model, (obj, (0.0,0.1), 1, (0.3, 0.2)), True)

    xs1 = caps_layer_1[:, dim]
    xs2 = caps_layer_2[:, dim]


    for x in range(6):      
        for y in range(6):
            i = x + y * 6 + capsule * 36
            c = 'r' if capsule == 0 else 'b'
            
            ax.scatter([x], [y], [xs1[i]], c=c, marker='x')
            ax.scatter([x], [y], [xs2[i]], c=c, marker='^')
            ax.plot([x, x], [y, y], zs=[xs1[i], xs2[i]], c=c)

    # Plot 0,0,0 lines
    ax.set_xlim3d([0,5])
    ax.set_ylim3d([0,5])
    ax.set_zlim3d([-1,1])

    plt.show()


def primary_layer_compare(model):
    """ This function can be used to debug vectors of sample data.
        It prints what a layer outputs for an input.
    """
    
    # Display points in 3D plot
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')
    ax.set_xlabel("DIM=1")
    ax.set_ylabel("DIM=2")
    ax.set_zlabel("DIM=3")

    caps_layer_1, _ = get_output_for_settings(model, (0, (0.0,0.0), 0, (0.4, 0.2)), debug=False)
    caps_layer_2, _ = get_output_for_settings(model, (1, (0.0,0.0), 0, (0.4, 0.2)), debug=False)
    
    xs1 = caps_layer_1[:, 0]
    ys1 = caps_layer_1[:, 1]
    zs1 = caps_layer_1[:, 2]
    ax.scatter(xs1, ys1, zs1, c='r', marker='x')

    xs2 = caps_layer_2[:, 0]
    ys2 = caps_layer_2[:, 1]
    zs2 = caps_layer_2[:, 2]
    ax.scatter(xs2, ys2, zs2, c='b', marker='^')

    # Plot 0,0,0 lines
    ax.set_xlim3d([-1,1])
    ax.set_ylim3d([-1,1])
    ax.set_zlim3d([-1,1])

    plt.show()


def get_output_for_settings(model, settings, debug=False):
    x, y = symmetric_dataset.generate_image(WIDTH, HEIGHT, settings)

    # Display image
    if debug:
        img = Image.fromarray(np.array(x).reshape(WIDTH, HEIGHT, 3))
        img.show()

    # Reshape for model
    x = np.array(x).reshape(-1, WIDTH, HEIGHT, 3).astype('float32') / 255

    # Little bit of debugging
    get_3rd_layer_output = K.function(
        [model.layers[0].input], 
        [model.layers[4].output, model.layers[5].output])

    layer_output = get_3rd_layer_output([x])
    return layer_output[0][0], layer_output[1][0]



#
# Main
#
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Capsule Network on MNIST.")
    parser.add_argument('--epochs', default=50, type=int)

    parser.add_argument('--batch_size', default=32, type=int)

    parser.add_argument('--max_num_samples', default=None, type=int,
                        help="Max. number of training examples to use. -1 to use all")

    parser.add_argument('--lr', default=0.001, type=float,
                        help="Initial learning rate")

    parser.add_argument('--lr_decay', default=0.9, type=float,
                        help="The value multiplied by lr at each epoch. Set a larger value for larger epochs")

    parser.add_argument('--scale_reconstruction_loss', default=1, type=float,
                        help="The coefficient for the loss of decoder")

    parser.add_argument('-r', '--num_routing', default=3, type=int,
                        help="Number of iterations used in routing algorithm. should > 0")

    parser.add_argument('--shift_fraction', default=0.1, type=float,
                        help="Fraction of pixels to shift at most in each direction.")

    parser.add_argument('--debug', action='store_true',
                        help="Save weights by TensorBoard")

    parser.add_argument('--save_dir', default='./result-capsnet')

    parser.add_argument('-t', '--testing', action='store_true',
                        help="Test the trained model on testing dataset")
    
    parser.add_argument('--rotation_range', default=0.0, type=float,
                        help="(TestOnly) Rotate the test dataset randomly in the given range in degrees.")

    parser.add_argument('--digit', default=5, type=int,
                        help="Digit to manipulate")

    parser.add_argument('--manipulate', default=0, type=int,
                        help="Vector to manipulate")

    parser.add_argument('-w', '--weights', default=None,
                        help="The path of the saved weights. Should be specified when testing")
    args = parser.parse_args()

    main(args)